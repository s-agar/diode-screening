#!/usr/bin/env python3
"""
Search IEEE Xplore for candidate SiC power-diode papers using the shared
Plan-C search helper.

Expected companion file in the same directory:
    sic_search_common.py

This script follows the same workflow conventions as the OpenAlex and Semantic
Scholar provider scripts:
- uses timestamped output filenames via sic_search_common.write_search_outputs()
- writes candidates CSV/RIS, skipped-existing audit CSV, and summary JSON
- checks Zotero Local API against the whole configured collection tree by DOI
  and conservative normalized title+year
- uses the shared SiC diode query set, including optional BFOM/FOM/Baliga
  figure-of-merit queries
- supports an API key from IEEE_API_KEY by default
- prints whether the API key appears to work at startup
- uses paced requests plus Retry-After-aware exponential backoff for transient
  failures and rate limiting

IEEE Xplore API notes:
- The Metadata Search API endpoint is:
    https://ieeexploreapi.ieee.org/api/v1/search/articles
- The API key is passed as the apikey query parameter.
- querytext performs a free-text search across configured metadata fields and
  abstract text.
- max_records is capped at 200 by the API; start_record is 1-based.
- This script defaults to max_records=200 and max_results_per_query=200 so each
  query normally uses one API call. This is important for accounts limited to
  200 calls/day.
- Default request spacing is intentionally conservative even though the rate
  ceiling may be higher, because the daily call budget is usually the limiting
  constraint.
"""

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from sic_search_common import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_ZOTERO_COLLECTION,
    clean_html,
    clean_space,
    collect_existing_zotero_records,
    dedupe_candidates,
    doi_link,
    infer_is_review,
    mark_zotero_duplicates,
    normalize_doi,
    query_specs,
    score_candidate,
    should_keep_candidate,
    write_search_outputs,
)


IEEE_SEARCH_URL = "https://ieeexploreapi.ieee.org/api/v1/search/articles"
PROVIDER_NAME = "ieee_xplore"
DEFAULT_API_KEY_ENV = "IEEE_API_KEY"
DEFAULT_MAX_RESULTS_PER_QUERY = 200
DEFAULT_MAX_RECORDS_PER_REQUEST = 200
DEFAULT_SLEEP_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_RETRIES = 6
DEFAULT_RETRY_SLEEP_SECONDS = 2.0
DEFAULT_RETRY_BACKOFF_BASE = 4.0
DEFAULT_MAX_RETRY_SLEEP_SECONDS = 32.0
DEFAULT_RETRY_JITTER_SECONDS = 1.0
DEFAULT_DAILY_CALL_BUDGET = 200

TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class RequestPacer:
    """Process-local minimum-interval request pacer."""

    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self.last_request_time = 0.0

    def wait(self) -> None:
        if self.min_interval_seconds <= 0:
            return
        now = time.monotonic()
        elapsed = now - self.last_request_time
        remaining = self.min_interval_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def mark_request(self) -> None:
        self.last_request_time = time.monotonic()


def parse_retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if value in [None, ""]:
        return None
    try:
        parsed = float(str(value).strip())
        if parsed >= 0:
            return parsed
    except Exception:
        return None
    return None


def retry_wait_seconds(
    attempt: int,
    retry_after_header: Optional[str],
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
) -> Tuple[float, str]:
    retry_after = parse_retry_after_seconds(retry_after_header)
    if retry_after is not None:
        return retry_after, "Retry-After"

    base_wait = max(0.0, retry_sleep_base_seconds) * (max(1.0, backoff_base) ** max(0, attempt - 1))
    capped_wait = min(max(0.0, max_retry_sleep_seconds), base_wait)
    jitter = random.uniform(0.0, max(0.0, jitter_seconds)) if jitter_seconds > 0 else 0.0
    return capped_wait + jitter, "exponential_backoff"


def ieee_safe_query(query: str) -> str:
    """Keep the shared query plain and API-friendly."""
    query = clean_space(query or "")
    query = query.replace("–", "-").replace("—", "-")
    return query


def ieee_request(
    params: dict,
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    pacer: RequestPacer,
    request_stats: dict,
) -> dict:
    attempts_allowed = max(1, int(max_retries))

    for attempt in range(1, attempts_allowed + 1):
        response = None
        request_stats["request_attempt_count"] = request_stats.get("request_attempt_count", 0) + 1

        try:
            call_budget = request_stats.get("daily_call_budget")
            if call_budget is not None and request_stats.get("request_attempt_count", 0) > int(call_budget):
                raise RuntimeError(
                    f"daily_call_budget_exceeded: attempted request "
                    f"{request_stats.get('request_attempt_count', 0)} of budget {call_budget}. "
                    "Use --daily-call-budget 0 to disable this guard."
                )

            pacer.wait()
            response = requests.get(IEEE_SEARCH_URL, params=params, timeout=timeout)
            pacer.mark_request()
        except requests.RequestException as exc:
            if attempt >= attempts_allowed:
                raise RuntimeError(f"request_exception after {attempt} attempt(s): {exc}") from exc

            sleep_seconds, sleep_source = retry_wait_seconds(
                attempt=attempt,
                retry_after_header=None,
                retry_sleep_base_seconds=retry_sleep_base_seconds,
                backoff_base=backoff_base,
                max_retry_sleep_seconds=max_retry_sleep_seconds,
                jitter_seconds=jitter_seconds,
            )
            request_stats["retry_count"] = request_stats.get("retry_count", 0) + 1
            print(
                f"  IEEE Xplore request exception on attempt {attempt}/{attempts_allowed}: {exc}. "
                f"Sleeping {sleep_seconds:g} seconds ({sleep_source})...",
                file=sys.stderr,
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            continue

        if response.status_code not in TRANSIENT_HTTP_STATUSES:
            response.raise_for_status()
            data = response.json()
            api_error = extract_ieee_api_error(data)
            if api_error:
                raise RuntimeError(api_error)
            return data

        if attempt >= attempts_allowed:
            response.raise_for_status()

        if response.status_code == 429:
            request_stats["rate_limit_count"] = request_stats.get("rate_limit_count", 0) + 1
        else:
            request_stats["transient_error_count"] = request_stats.get("transient_error_count", 0) + 1

        sleep_seconds, sleep_source = retry_wait_seconds(
            attempt=attempt,
            retry_after_header=response.headers.get("Retry-After"),
            retry_sleep_base_seconds=retry_sleep_base_seconds,
            backoff_base=backoff_base,
            max_retry_sleep_seconds=max_retry_sleep_seconds,
            jitter_seconds=jitter_seconds,
        )
        request_stats["retry_count"] = request_stats.get("retry_count", 0) + 1
        print(
            f"  IEEE Xplore returned HTTP {response.status_code} on attempt "
            f"{attempt}/{attempts_allowed}. Sleeping {sleep_seconds:g} seconds ({sleep_source})...",
            file=sys.stderr,
        )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    raise RuntimeError("IEEE Xplore request failed after retries")


def extract_ieee_api_error(data: object) -> str:
    if not isinstance(data, dict):
        return ""

    for key in ["error", "errors", "message", "messages"]:
        value = data.get(key)
        if not value:
            continue
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)

    return ""


def ieee_total_found(data: dict) -> int:
    for key in ["total_records", "totalfound", "total_found", "totalsearched"]:
        value = data.get(key)
        try:
            return int(value)
        except Exception:
            continue
    articles = data.get("articles") or []
    return len(articles) if isinstance(articles, list) else 0


def normalize_authors(article: dict) -> str:
    authors = article.get("authors") or article.get("authors_list") or ""

    if isinstance(authors, dict):
        nested = authors.get("authors") or authors.get("author") or []
        names = []
        if isinstance(nested, list):
            for author in nested:
                if isinstance(author, dict):
                    name = clean_space(author.get("full_name") or author.get("name") or author.get("preferred_name") or "")
                    if name:
                        names.append(name)
                else:
                    name = clean_space(author)
                    if name:
                        names.append(name)
        if names:
            return "; ".join(names)

    if isinstance(authors, list):
        names = []
        for author in authors:
            if isinstance(author, dict):
                name = clean_space(author.get("full_name") or author.get("name") or author.get("preferred_name") or "")
            else:
                name = clean_space(author)
            if name:
                names.append(name)
        return "; ".join(names)

    return clean_space(authors)


def normalize_index_terms(article: dict) -> str:
    index_terms = article.get("index_terms") or {}
    values = []

    if isinstance(index_terms, dict):
        for _, group_value in index_terms.items():
            if isinstance(group_value, dict):
                terms = group_value.get("terms") or group_value.get("term") or []
            else:
                terms = group_value

            if isinstance(terms, list):
                values.extend(clean_space(term) for term in terms if clean_space(term))
            elif terms:
                values.append(clean_space(terms))
    elif isinstance(index_terms, list):
        values.extend(clean_space(term) for term in index_terms if clean_space(term))
    elif index_terms:
        values.append(clean_space(index_terms))

    ieee_terms = article.get("ieee_terms") or []
    author_terms = article.get("author_terms") or []
    for terms in [ieee_terms, author_terms]:
        if isinstance(terms, list):
            values.extend(clean_space(term) for term in terms if clean_space(term))
        elif terms:
            values.append(clean_space(terms))

    deduped = []
    seen = set()
    for value in values:
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            deduped.append(value)

    return "; ".join(deduped)


def ieee_article_url(article: dict) -> str:
    for key in ["html_url", "abstract_url", "pdf_url", "url"]:
        value = clean_space(article.get(key) or "")
        if value:
            return value

    article_number = clean_space(article.get("article_number") or "")
    if article_number:
        return f"https://ieeexplore.ieee.org/document/{article_number}"

    return ""


def ieee_article_title(article: dict) -> str:
    return clean_html(article.get("title") or article.get("article_title") or article.get("document_title") or "")


def ieee_article_abstract(article: dict) -> str:
    return clean_html(article.get("abstract") or article.get("abstract_text") or "")


def ieee_article_year(article: dict) -> str:
    year = article.get("publication_year") or article.get("year") or ""
    if year:
        return clean_space(year)
    publication_date = clean_space(article.get("publication_date") or "")
    match = re.search(r"\b((?:19|20)\d{2})\b", publication_date)
    return match.group(1) if match else ""


def ieee_article_to_candidate(article: dict, query_family: str, query: str) -> Optional[dict]:
    title = ieee_article_title(article)
    abstract = ieee_article_abstract(article)

    if not should_keep_candidate(title, abstract):
        return None

    doi = normalize_doi(article.get("doi") or article.get("DOI") or "")
    year = ieee_article_year(article)
    publication_date = clean_space(article.get("publication_date") or "")
    venue = clean_html(article.get("publication_title") or article.get("publication") or "")
    article_number = clean_space(article.get("article_number") or article.get("arnumber") or "")
    url = ieee_article_url(article)
    content_type = clean_space(article.get("content_type") or article.get("contentType") or "")
    publisher = clean_space(article.get("publisher") or "")
    index_terms = normalize_index_terms(article)

    scoring_abstract = abstract
    if index_terms:
        scoring_abstract = f"{abstract} {index_terms}".strip()

    return {
        "score": score_candidate(title, scoring_abstract),
        "database_source": "IEEE Xplore",
        "provider_id": article_number,
        "provider_url": url,
        "title": title,
        "doi": doi,
        "doi_link": doi_link(doi),
        "year": year,
        "publication_date": publication_date,
        "venue": venue,
        "authors": normalize_authors(article),
        "abstract": abstract,
        "url": url,
        "work_type": content_type or publisher,
        "is_review": infer_is_review(content_type, title, abstract),
        "cited_by_count": article.get("citing_paper_count") or 0,
        "query_family": query_family,
        "query": query,
        "already_in_zotero": False,
        "skip_reason": "",
    }


def ieee_search_page(
    query: str,
    api_key: str,
    start_record: int,
    max_records: int,
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    pacer: RequestPacer,
    request_stats: dict,
) -> dict:
    params = {
        "apikey": api_key,
        "format": "json",
        "querytext": ieee_safe_query(query),
        "max_records": str(max(1, min(int(max_records), 200))),
        "start_record": str(max(1, int(start_record))),
    }

    return ieee_request(
        params=params,
        timeout=timeout,
        max_retries=max_retries,
        retry_sleep_base_seconds=retry_sleep_base_seconds,
        backoff_base=backoff_base,
        max_retry_sleep_seconds=max_retry_sleep_seconds,
        jitter_seconds=jitter_seconds,
        pacer=pacer,
        request_stats=request_stats,
    )


def check_ieee_api_key(
    api_key: str,
    api_key_env: str,
    timeout: int,
    pacer: RequestPacer,
    request_stats: Optional[dict] = None,
) -> dict:
    result = {
        "api_key_env": api_key_env,
        "api_key_present": bool(api_key),
        "api_key_check_status": "not_checked",
        "api_key_check_http_status": "",
        "api_key_check_message": "",
    }

    if not api_key:
        message = (
            f"IEEE Xplore API key check: no key found in {api_key_env}. "
            f"Set {api_key_env} before running this script."
        )
        print(message, file=sys.stderr)
        result["api_key_check_status"] = "missing"
        result["api_key_check_message"] = message
        return result

    params = {
        "apikey": api_key,
        "format": "json",
        "querytext": "SiC diode",
        "max_records": "1",
        "start_record": "1",
    }

    try:
        if request_stats is not None:
            request_stats["request_attempt_count"] = request_stats.get("request_attempt_count", 0) + 1
            call_budget = request_stats.get("daily_call_budget")
            if call_budget is not None and request_stats.get("request_attempt_count", 0) > int(call_budget):
                raise RuntimeError(
                    f"daily_call_budget_exceeded during API-key check: attempted request "
                    f"{request_stats.get('request_attempt_count', 0)} of budget {call_budget}. "
                    "Use --daily-call-budget 0 to disable this guard."
                )

        pacer.wait()
        response = requests.get(IEEE_SEARCH_URL, params=params, timeout=timeout)
        pacer.mark_request()
    except requests.RequestException as exc:
        message = f"IEEE Xplore API key check: request failed: {exc}"
        print(message, file=sys.stderr)
        result["api_key_check_status"] = "request_failed"
        result["api_key_check_message"] = message
        return result

    result["api_key_check_http_status"] = response.status_code

    try:
        data = response.json()
    except Exception:
        data = None

    api_error = extract_ieee_api_error(data)

    if response.status_code == 200 and not api_error:
        total_found = ieee_total_found(data if isinstance(data, dict) else {})
        message = (
            f"IEEE Xplore API key check: key found in {api_key_env} and accepted "
            f"by IEEE Xplore (HTTP 200; startup query total found: {total_found})."
        )
        print(message)
        result["api_key_check_status"] = "accepted"
        result["api_key_check_message"] = message
        return result

    if response.status_code in {401, 403}:
        message = (
            f"IEEE Xplore API key check: key found in {api_key_env}, but IEEE Xplore "
            f"returned HTTP {response.status_code}. The key may be invalid, expired, or unauthorized."
        )
    elif response.status_code == 429:
        message = (
            f"IEEE Xplore API key check: key found in {api_key_env}, but startup check was "
            "rate-limited (HTTP 429). The key may still be valid; continuing with paced retries."
        )
    elif api_error:
        message = f"IEEE Xplore API key check: API returned an error: {api_error}"
    else:
        body = response.text[:300] if response.text else ""
        message = (
            f"IEEE Xplore API key check: unexpected HTTP {response.status_code}. "
            f"Response body starts: {body}"
        )

    print(message, file=sys.stderr)
    result["api_key_check_status"] = "error_or_unexpected_status"
    result["api_key_check_message"] = message
    return result


def search_query(
    query_family: str,
    query: str,
    api_key: str,
    max_results: int,
    max_records_per_request: int,
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    pacer: RequestPacer,
    request_stats: dict,
) -> Tuple[List[dict], int]:
    candidates = []
    total_found = 0
    inspected = 0
    start_record = 1
    page_size = max(1, min(int(max_records_per_request), 200))
    max_results = max(1, int(max_results))

    while inspected < max_results:
        records_to_fetch = min(page_size, max_results - inspected)
        data = ieee_search_page(
            query=query,
            api_key=api_key,
            start_record=start_record,
            max_records=records_to_fetch,
            timeout=timeout,
            max_retries=max_retries,
            retry_sleep_base_seconds=retry_sleep_base_seconds,
            backoff_base=backoff_base,
            max_retry_sleep_seconds=max_retry_sleep_seconds,
            jitter_seconds=jitter_seconds,
            pacer=pacer,
            request_stats=request_stats,
        )

        if not total_found:
            total_found = ieee_total_found(data)

        articles = data.get("articles") or []
        if not isinstance(articles, list) or not articles:
            break

        kept_this_page = 0
        for article in articles:
            if not isinstance(article, dict):
                continue
            candidate = ieee_article_to_candidate(article, query_family=query_family, query=query)
            if candidate:
                candidates.append(candidate)
                kept_this_page += 1

        inspected += len(articles)
        print(
            f"  start_record {start_record}: {len(articles)} records returned; "
            f"{kept_this_page} passed local relevance filter"
        )

        if len(articles) < records_to_fetch:
            break
        if total_found and start_record + len(articles) > total_found:
            break

        start_record += len(articles)

    return candidates, total_found


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search IEEE Xplore for SiC power-diode candidate papers with Zotero duplicate filtering."
    )

    parser.add_argument(
        "--collection",
        default=DEFAULT_ZOTERO_COLLECTION,
        help=f"Zotero collection name, path, or key to de-duplicate against, including subcollections. Default: {DEFAULT_ZOTERO_COLLECTION}",
    )

    parser.add_argument(
        "--outdir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )

    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=f"Environment variable containing the IEEE Xplore API key. Default: {DEFAULT_API_KEY_ENV}",
    )

    parser.add_argument(
        "--api-key",
        default="",
        help="IEEE Xplore API key. Prefer using --api-key-env to avoid putting keys in shell history.",
    )

    parser.add_argument(
        "--skip-api-key-check",
        action="store_true",
        help="Skip the startup request that checks whether the IEEE Xplore API key appears accepted.",
    )

    parser.add_argument(
        "--max-results-per-query",
        type=int,
        default=DEFAULT_MAX_RESULTS_PER_QUERY,
        help=f"Maximum IEEE Xplore records to inspect per query. Default: {DEFAULT_MAX_RESULTS_PER_QUERY}",
    )

    parser.add_argument(
        "--max-records-per-request",
        type=int,
        default=DEFAULT_MAX_RECORDS_PER_REQUEST,
        help=f"IEEE max_records per request, capped at 200 by the API. Default: {DEFAULT_MAX_RECORDS_PER_REQUEST}",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"Minimum seconds between IEEE Xplore requests. Default: {DEFAULT_SLEEP_SECONDS}",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout per request in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help=f"Maximum attempts for rate limits, transient errors, and request exceptions. Default: {DEFAULT_MAX_RETRIES}",
    )

    parser.add_argument(
        "--retry-sleep",
        type=float,
        default=DEFAULT_RETRY_SLEEP_SECONDS,
        help=f"Initial seconds for exponential backoff when Retry-After is absent. Default: {DEFAULT_RETRY_SLEEP_SECONDS}",
    )

    parser.add_argument(
        "--retry-backoff-base",
        type=float,
        default=DEFAULT_RETRY_BACKOFF_BASE,
        help=f"Exponential backoff multiplier when Retry-After is absent. Default: {DEFAULT_RETRY_BACKOFF_BASE}",
    )

    parser.add_argument(
        "--max-retry-sleep",
        type=float,
        default=DEFAULT_MAX_RETRY_SLEEP_SECONDS,
        help=(
            f"Maximum exponential-backoff sleep in seconds when Retry-After is absent. "
            f"Retry-After is honored even above this value. Default: {DEFAULT_MAX_RETRY_SLEEP_SECONDS}"
        ),
    )

    parser.add_argument(
        "--retry-jitter",
        type=float,
        default=DEFAULT_RETRY_JITTER_SECONDS,
        help=f"Random jitter, in seconds, added to non-Retry-After retry sleeps. Default: {DEFAULT_RETRY_JITTER_SECONDS}",
    )

    parser.add_argument(
        "--daily-call-budget",
        type=int,
        default=DEFAULT_DAILY_CALL_BUDGET,
        help=(
            f"Safety guard for total IEEE API request attempts this run, including the API-key check and retries. "
            f"Use 0 to disable. Default: {DEFAULT_DAILY_CALL_BUDGET}"
        ),
    )

    parser.add_argument(
        "--no-bfom-queries",
        action="store_true",
        help="Disable the additional Baliga/BFOM/FOM query family.",
    )

    parser.add_argument(
        "--limit-queries",
        type=int,
        default=0,
        help="Optional maximum number of queries to run. 0 means all queries. Useful for testing.",
    )

    parser.add_argument(
        "--skip-zotero-check",
        action="store_true",
        help="Do not check Zotero for existing DOI/title+year matches. Not recommended for production use.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    api_key = clean_space(args.api_key or os.environ.get(args.api_key_env, ""))
    pacer = RequestPacer(min_interval_seconds=max(0.0, args.sleep))
    request_stats = {
        "request_attempt_count": 0,
        "retry_count": 0,
        "rate_limit_count": 0,
        "transient_error_count": 0,
        "daily_call_budget": max(0, int(args.daily_call_budget)) or None,
    }

    if args.skip_api_key_check:
        api_key_check = {
            "api_key_env": args.api_key_env,
            "api_key_present": bool(api_key),
            "api_key_check_status": "skipped",
            "api_key_check_http_status": "",
            "api_key_check_message": "IEEE Xplore API key startup check skipped by --skip-api-key-check.",
        }
        print(api_key_check["api_key_check_message"])
        if api_key:
            print("IEEE Xplore API key presence: key provided, but not verified.")
        else:
            print(f"IEEE Xplore API key presence: no key found in {args.api_key_env}.", file=sys.stderr)
    else:
        api_key_check = check_ieee_api_key(
            api_key=api_key,
            api_key_env=args.api_key_env,
            timeout=args.timeout,
            pacer=pacer,
            request_stats=request_stats,
        )

    if not api_key:
        raise RuntimeError(
            f"No IEEE Xplore API key provided. Set {args.api_key_env} or pass --api-key."
        )

    specs = query_specs(include_bfom=not args.no_bfom_queries)
    if args.limit_queries and args.limit_queries > 0:
        specs = specs[:args.limit_queries]

    existing_zotero_dois: Dict[str, dict] = {}
    existing_zotero_title_years: Dict[str, dict] = {}
    if args.skip_zotero_check:
        print("WARNING: Zotero duplicate check is disabled.", file=sys.stderr)
    else:
        print(f"Checking Zotero collection tree: {args.collection}")
        existing_zotero_records = collect_existing_zotero_records(args.collection)
        existing_zotero_dois = existing_zotero_records.get("dois", {})
        existing_zotero_title_years = existing_zotero_records.get("title_years", {})
        print(f"Found {len(existing_zotero_dois)} existing Zotero DOI(s) in collection tree.")
        print(f"Found {len(existing_zotero_title_years)} existing Zotero title+year key(s) in collection tree.")

    print(f"Running {len(specs)} IEEE Xplore quer{'y' if len(specs) == 1 else 'ies'}.")
    print(f"IEEE Xplore request pacing: at least {max(0.0, args.sleep):g} seconds between request attempts.")
    print(
        "IEEE Xplore retry policy: "
        f"max_retries={max(1, args.max_retries)}, "
        f"retry_sleep={max(0.0, args.retry_sleep):g}, "
        f"retry_backoff_base={max(1.0, args.retry_backoff_base):g}, "
        f"max_retry_sleep={max(0.0, args.max_retry_sleep):g}; "
        "server Retry-After is honored when provided."
    )

    daily_budget = max(0, int(args.daily_call_budget))
    estimated_nominal_calls = len(specs)
    if not args.skip_api_key_check:
        estimated_nominal_calls += 1
    print(
        f"IEEE Xplore daily-call guard: "
        f"{daily_budget if daily_budget else 'disabled'}; "
        f"nominal planned calls without retries: {estimated_nominal_calls}."
    )
    if daily_budget and estimated_nominal_calls > daily_budget:
        print(
            f"WARNING: nominal planned calls ({estimated_nominal_calls}) exceed --daily-call-budget "
            f"({daily_budget}). The script will stop once the guard is reached. "
            "Use --limit-queries or disable some query families to stay within budget.",
            file=sys.stderr,
        )

    raw_candidates = []
    query_errors = []
    query_totals = []

    for index, (family, query) in enumerate(specs, start=1):
        call_budget = request_stats.get("daily_call_budget")
        if call_budget is not None and request_stats.get("request_attempt_count", 0) >= int(call_budget):
            message = (
                f"daily_call_budget_reached before query {index}/{len(specs)}: "
                f"{request_stats.get('request_attempt_count', 0)} request attempts used out of {call_budget}."
            )
            print(f"  STOPPING: {message}", file=sys.stderr)
            query_errors.append(message)
            break

        print(f"\n[{index}/{len(specs)}] {family}: {query}")
        try:
            candidates, total_found = search_query(
                query_family=family,
                query=query,
                api_key=api_key,
                max_results=max(1, args.max_results_per_query),
                max_records_per_request=max(1, args.max_records_per_request),
                timeout=args.timeout,
                max_retries=max(1, args.max_retries),
                retry_sleep_base_seconds=max(0.0, args.retry_sleep),
                backoff_base=max(1.0, args.retry_backoff_base),
                max_retry_sleep_seconds=max(0.0, args.max_retry_sleep),
                jitter_seconds=max(0.0, args.retry_jitter),
                pacer=pacer,
                request_stats=request_stats,
            )
            raw_candidates.extend(candidates)
            query_totals.append({
                "query_family": family,
                "query": query,
                "total_found": total_found,
                "kept_after_local_relevance_filter": len(candidates),
            })
            print(f"  total found reported by IEEE Xplore: {total_found}")
            print(f"  kept after local relevance filter: {len(candidates)}")
        except Exception as exc:
            message = f"{family}: {query}: {exc}"
            print(f"  ERROR: {message}", file=sys.stderr)
            query_errors.append(message)

    deduped = dedupe_candidates(raw_candidates)
    kept, skipped = mark_zotero_duplicates(deduped, existing_zotero_dois, existing_zotero_title_years)

    paths = write_search_outputs(
        candidates=kept,
        skipped=skipped,
        outdir=outdir,
        provider_name=PROVIDER_NAME,
        summary_extra={
            "provider": "IEEE Xplore",
            "api_url": IEEE_SEARCH_URL,
            "api_key_env": args.api_key_env,
            "api_key_used": bool(api_key),
            **api_key_check,
            "raw_candidate_count_before_dedupe": len(raw_candidates),
            "deduped_candidate_count_before_zotero_skip": len(deduped),
            "query_count": len(specs),
            "query_errors": query_errors,
            "query_totals": query_totals,
            "zotero_collection": args.collection,
            "zotero_duplicate_check_enabled": not args.skip_zotero_check,
            "existing_zotero_doi_count": len(existing_zotero_dois),
            "existing_zotero_title_year_count": len(existing_zotero_title_years),
            "bfom_queries_enabled": not args.no_bfom_queries,
            "max_results_per_query": max(1, args.max_results_per_query),
            "max_records_per_request": max(1, min(args.max_records_per_request, 200)),
            "sleep_seconds": max(0.0, args.sleep),
            "retry_sleep_seconds": max(0.0, args.retry_sleep),
            "retry_backoff_base": max(1.0, args.retry_backoff_base),
            "max_retry_sleep_seconds": max(0.0, args.max_retry_sleep),
            "retry_jitter_seconds": max(0.0, args.retry_jitter),
            "max_retries": max(1, args.max_retries),
            "daily_call_budget": max(0, int(args.daily_call_budget)),
            "nominal_planned_calls_without_retries": estimated_nominal_calls,
            "request_attempt_count": request_stats.get("request_attempt_count", 0),
            "retry_count": request_stats.get("retry_count", 0),
            "rate_limit_count": request_stats.get("rate_limit_count", 0),
            "transient_error_count": request_stats.get("transient_error_count", 0),
        },
    )

    print("\nDone.")
    print(f"Raw candidate hits after local relevance filter: {len(raw_candidates)}")
    print(f"Deduped before Zotero skip: {len(deduped)}")
    print(f"Candidates written: {len(kept)}")
    print(f"Skipped because DOI or title+year exists in Zotero: {len(skipped)}")
    print(f"IEEE Xplore request attempts: {request_stats.get('request_attempt_count', 0)}")
    print(f"IEEE Xplore daily-call budget guard: {max(0, int(args.daily_call_budget)) or 'disabled'}")
    print(f"IEEE Xplore retries: {request_stats.get('retry_count', 0)}")
    print(f"IEEE Xplore HTTP 429 rate limits: {request_stats.get('rate_limit_count', 0)}")
    print(f"Candidates CSV: {paths['candidates_csv']}")
    print(f"Candidates RIS: {paths['candidates_ris']}")
    print(f"Skipped audit CSV: {paths['skipped_csv']}")
    print(f"Summary JSON: {paths['summary_json']}")

    if query_errors:
        print("\nSome queries failed; see summary JSON for details.", file=sys.stderr)


if __name__ == "__main__":
    main()
