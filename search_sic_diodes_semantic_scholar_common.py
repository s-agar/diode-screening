#!/usr/bin/env python3
"""
Search Semantic Scholar for candidate SiC power-diode papers.

This script complements the OpenAlex search script and uses the shared helpers in
sic_search_common.py for:
- query construction, including Baliga figure-of-merit / BFOM / FOM variants
- Zotero Local API duplicate checking against "SiC Screening" and subcollections
- DOI normalization
- candidate scoring and filtering
- timestamped CSV/RIS output
- skipped-existing-in-Zotero audit output

Default output files look like:
  sic_diode_search_results/semantic_scholar_YYYYMMDD_HHMMSS_candidates.csv
  sic_diode_search_results/semantic_scholar_YYYYMMDD_HHMMSS_candidates.ris
  sic_diode_search_results/semantic_scholar_YYYYMMDD_HHMMSS_skipped_existing_in_zotero.csv
  sic_diode_search_results/semantic_scholar_YYYYMMDD_HHMMSS_summary.json

Semantic Scholar API notes:
- An API key is optional but strongly recommended.
- If you have one, set it as an environment variable:
    export S2_API_KEY="your_key_here"
- Authenticated requests must send the key in the case-sensitive x-api-key header.
- Semantic Scholar currently advises using bulk/batch endpoints when retrieving
  larger result sets and limiting fields to only what is needed.
- API-key traffic is still rate limited, so this script uses pacing plus
  Retry-After-aware exponential backoff for HTTP 429 and transient server errors.
"""

import argparse
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


SEMANTIC_SCHOLAR_RELEVANCE_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
SEMANTIC_SCHOLAR_BULK_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
PROVIDER_NAME = "semantic_scholar"

# Keep this list intentionally limited. Semantic Scholar explicitly recommends
# requesting only fields you need because larger field sets can slow responses
# and make rate-limit issues worse.
SEMANTIC_SCHOLAR_FIELDS = ",".join([
    "paperId",
    "title",
    "year",
    "publicationDate",
    "authors",
    "venue",
    "journal",
    "abstract",
    "url",
    "externalIds",
    "citationCount",
    "openAccessPdf",
    "publicationTypes",
])

TRANSIENT_HTTP_STATUSES = {429, 500, 502, 503, 504}


class SemanticScholarRateLimiter:
    """Simple process-local pacer for Semantic Scholar requests."""

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


def semantic_scholar_safe_query(query: str) -> str:
    """
    Semantic Scholar search is plain-text search. Hyphen-like punctuation can
    behave badly in some search contexts, so normalize it to spaces.
    """
    query = query or ""
    query = query.replace("-", " ")
    query = query.replace("–", " ")
    query = query.replace("—", " ")
    query = re.sub(r"\s+", " ", query).strip()
    return query


def semantic_scholar_headers(api_key: Optional[str]) -> dict:
    headers = {
        "User-Agent": "sic-diode-literature-mining/1.0",
        "Accept": "application/json",
    }
    if api_key:
        headers["x-api-key"] = api_key
    return headers


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


def retry_sleep_seconds(
    attempt: int,
    retry_after_header: Optional[str],
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
) -> Tuple[float, str]:
    """
    Compute retry wait time.

    If the server provides Retry-After, honor it even if it exceeds the local
    max-retry-sleep setting. Otherwise use capped exponential backoff:
        retry_sleep_base_seconds * backoff_base ** (attempt - 1)
    plus a small optional jitter.
    """
    retry_after = parse_retry_after_seconds(retry_after_header)
    if retry_after is not None:
        return max(0.0, retry_after), "Retry-After"

    base_wait = max(0.0, retry_sleep_base_seconds) * (max(1.0, backoff_base) ** max(0, attempt - 1))
    capped_wait = min(max(0.0, max_retry_sleep_seconds), base_wait)
    jitter = random.uniform(0.0, max(0.0, jitter_seconds)) if jitter_seconds > 0 else 0.0
    return capped_wait + jitter, "exponential_backoff"


def semantic_scholar_request(
    url: str,
    params: dict,
    api_key: Optional[str],
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    rate_limiter: SemanticScholarRateLimiter,
    request_stats: dict,
) -> dict:
    headers = semantic_scholar_headers(api_key)
    attempts_allowed = max(1, int(max_retries))

    for attempt in range(1, attempts_allowed + 1):
        response = None
        request_stats["request_attempt_count"] = request_stats.get("request_attempt_count", 0) + 1

        try:
            rate_limiter.wait()
            response = requests.get(url, params=params, headers=headers, timeout=timeout)
            rate_limiter.mark_request()
        except requests.RequestException as exc:
            if attempt >= attempts_allowed:
                raise RuntimeError(f"request_exception after {attempt} attempt(s): {exc}") from exc

            sleep_seconds, sleep_source = retry_sleep_seconds(
                attempt=attempt,
                retry_after_header=None,
                retry_sleep_base_seconds=retry_sleep_base_seconds,
                backoff_base=backoff_base,
                max_retry_sleep_seconds=max_retry_sleep_seconds,
                jitter_seconds=jitter_seconds,
            )
            request_stats["retry_count"] = request_stats.get("retry_count", 0) + 1
            print(
                f"  Semantic Scholar request exception on attempt {attempt}/{attempts_allowed}: {exc}. "
                f"Sleeping {sleep_seconds:g} seconds ({sleep_source})...",
                file=sys.stderr,
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)
            continue

        if response.status_code not in TRANSIENT_HTTP_STATUSES:
            response.raise_for_status()
            return response.json()

        if attempt >= attempts_allowed:
            response.raise_for_status()

        if response.status_code == 429:
            request_stats["rate_limit_count"] = request_stats.get("rate_limit_count", 0) + 1
        else:
            request_stats["transient_error_count"] = request_stats.get("transient_error_count", 0) + 1

        sleep_seconds, sleep_source = retry_sleep_seconds(
            attempt=attempt,
            retry_after_header=response.headers.get("Retry-After"),
            retry_sleep_base_seconds=retry_sleep_base_seconds,
            backoff_base=backoff_base,
            max_retry_sleep_seconds=max_retry_sleep_seconds,
            jitter_seconds=jitter_seconds,
        )
        request_stats["retry_count"] = request_stats.get("retry_count", 0) + 1

        print(
            f"  Semantic Scholar returned HTTP {response.status_code} on attempt "
            f"{attempt}/{attempts_allowed}. Sleeping {sleep_seconds:g} seconds ({sleep_source})...",
            file=sys.stderr,
        )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    raise RuntimeError("Semantic Scholar request failed after retries")


def check_semantic_scholar_api_key(
    api_key: str,
    api_key_env: str,
    timeout: int,
    rate_limiter: SemanticScholarRateLimiter,
) -> dict:
    """Perform a lightweight startup check and print whether the key appears usable."""
    result = {
        "api_key_env": api_key_env,
        "api_key_present": bool(api_key),
        "api_key_check_status": "not_checked",
        "api_key_check_http_status": "",
        "api_key_check_message": "",
    }

    if not api_key:
        message = (
            f"Semantic Scholar API key check: no key found in {api_key_env}. "
            "Requests will be unauthenticated and may be much more rate-limited."
        )
        print(message)
        result["api_key_check_status"] = "missing"
        result["api_key_check_message"] = message
        return result

    params = {
        "query": "SiC diode",
        "fields": "paperId",
        "year": "2020-",
    }

    try:
        rate_limiter.wait()
        response = requests.get(
            SEMANTIC_SCHOLAR_BULK_SEARCH_URL,
            params=params,
            headers=semantic_scholar_headers(api_key),
            timeout=timeout,
        )
        rate_limiter.mark_request()
    except requests.RequestException as exc:
        message = (
            f"Semantic Scholar API key check: key is present in {api_key_env}, "
            f"but the startup check request failed: {exc}"
        )
        print(message, file=sys.stderr)
        result["api_key_check_status"] = "request_failed"
        result["api_key_check_message"] = message
        return result

    result["api_key_check_http_status"] = response.status_code

    if response.status_code == 200:
        message = (
            f"Semantic Scholar API key check: key found in {api_key_env} and accepted "
            "by Semantic Scholar (HTTP 200)."
        )
        print(message)
        result["api_key_check_status"] = "accepted"
        result["api_key_check_message"] = message
        return result

    if response.status_code in {401, 403}:
        message = (
            f"Semantic Scholar API key check: key found in {api_key_env}, but Semantic Scholar "
            f"returned HTTP {response.status_code}. The key may be invalid, expired, or unauthorized."
        )
        print(message, file=sys.stderr)
        result["api_key_check_status"] = "rejected"
        result["api_key_check_message"] = message
        return result

    if response.status_code == 429:
        message = (
            f"Semantic Scholar API key check: key found in {api_key_env}, but the check was rate-limited "
            "(HTTP 429). The key may still be valid; continuing with paced retries."
        )
        print(message, file=sys.stderr)
        result["api_key_check_status"] = "rate_limited"
        result["api_key_check_message"] = message
        return result

    message = (
        f"Semantic Scholar API key check: key found in {api_key_env}, but startup check returned "
        f"HTTP {response.status_code}: {response.text[:300]}"
    )
    print(message, file=sys.stderr)
    result["api_key_check_status"] = "unexpected_status"
    result["api_key_check_message"] = message
    return result


def semantic_scholar_bulk_get(
    query: str,
    token: Optional[str],
    api_key: Optional[str],
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    rate_limiter: SemanticScholarRateLimiter,
    request_stats: dict,
) -> dict:
    params = {
        "query": semantic_scholar_safe_query(query),
        "fields": SEMANTIC_SCHOLAR_FIELDS,
    }
    if token:
        params["token"] = token

    return semantic_scholar_request(
        url=SEMANTIC_SCHOLAR_BULK_SEARCH_URL,
        params=params,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
        retry_sleep_base_seconds=retry_sleep_base_seconds,
        backoff_base=backoff_base,
        max_retry_sleep_seconds=max_retry_sleep_seconds,
        jitter_seconds=jitter_seconds,
        rate_limiter=rate_limiter,
        request_stats=request_stats,
    )


def semantic_scholar_relevance_get(
    query: str,
    limit: int,
    offset: int,
    api_key: Optional[str],
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    rate_limiter: SemanticScholarRateLimiter,
    request_stats: dict,
) -> dict:
    params = {
        "query": semantic_scholar_safe_query(query),
        "limit": limit,
        "offset": offset,
        "fields": SEMANTIC_SCHOLAR_FIELDS,
    }

    return semantic_scholar_request(
        url=SEMANTIC_SCHOLAR_RELEVANCE_SEARCH_URL,
        params=params,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
        retry_sleep_base_seconds=retry_sleep_base_seconds,
        backoff_base=backoff_base,
        max_retry_sleep_seconds=max_retry_sleep_seconds,
        jitter_seconds=jitter_seconds,
        rate_limiter=rate_limiter,
        request_stats=request_stats,
    )


def author_names(paper: dict) -> str:
    authors = []
    for author in paper.get("authors") or []:
        name = clean_space(author.get("name") or "")
        if name:
            authors.append(name)
    return "; ".join(authors)


def paper_doi(paper: dict) -> str:
    external_ids = paper.get("externalIds") or {}
    return normalize_doi(external_ids.get("DOI") or external_ids.get("doi") or "")


def paper_venue(paper: dict) -> str:
    venue = clean_space(paper.get("venue") or "")
    journal = paper.get("journal") or {}
    journal_name = clean_space(journal.get("name") or "") if isinstance(journal, dict) else ""
    return venue or journal_name


def paper_url(paper: dict) -> str:
    open_access_pdf = paper.get("openAccessPdf") or {}
    if isinstance(open_access_pdf, dict) and open_access_pdf.get("url"):
        return clean_space(open_access_pdf.get("url"))
    return clean_space(paper.get("url") or "")


def paper_work_type(paper: dict) -> str:
    publication_types = paper.get("publicationTypes") or []
    if isinstance(publication_types, list) and publication_types:
        return "; ".join(clean_space(value) for value in publication_types if clean_space(value))
    return ""


def map_paper_to_candidate(paper: dict, query_family: str, query: str) -> Optional[dict]:
    title = clean_html(paper.get("title") or "")
    abstract = clean_html(paper.get("abstract") or "")

    if not should_keep_candidate(title, abstract):
        return None

    doi = paper_doi(paper)
    work_type = paper_work_type(paper)
    score = score_candidate(title, abstract)
    provider_id = clean_space(paper.get("paperId") or "")
    provider_url = clean_space(paper.get("url") or "")

    candidate = {
        "score": score,
        "database_source": "Semantic Scholar",
        "provider_id": provider_id,
        "provider_url": provider_url,
        "title": title,
        "doi": doi,
        "doi_link": doi_link(doi),
        "year": paper.get("year") or "",
        "publication_date": paper.get("publicationDate") or "",
        "venue": paper_venue(paper),
        "authors": author_names(paper),
        "abstract": abstract,
        "url": paper_url(paper),
        "work_type": work_type,
        "is_review": infer_is_review(work_type, title, abstract),
        "cited_by_count": paper.get("citationCount") or 0,
        "query_family": query_family,
        "query": query,
        "already_in_zotero": False,
        "skip_reason": "",
    }

    return candidate


def search_query_bulk(
    query_family: str,
    query: str,
    max_results: int,
    api_key: Optional[str],
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    rate_limiter: SemanticScholarRateLimiter,
    request_stats: dict,
) -> List[dict]:
    candidates = []
    inspected = 0
    token = None

    while inspected < max_results:
        data = semantic_scholar_bulk_get(
            query=query,
            token=token,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            retry_sleep_base_seconds=retry_sleep_base_seconds,
            backoff_base=backoff_base,
            max_retry_sleep_seconds=max_retry_sleep_seconds,
            jitter_seconds=jitter_seconds,
            rate_limiter=rate_limiter,
            request_stats=request_stats,
        )

        papers = data.get("data") or []
        if not papers:
            break

        remaining = max_results - inspected
        for paper in papers[:remaining]:
            candidate = map_paper_to_candidate(paper, query_family=query_family, query=query)
            if candidate:
                candidates.append(candidate)

        inspected += min(len(papers), remaining)

        token = data.get("token")
        if not token:
            break

    return candidates


def search_query_relevance(
    query_family: str,
    query: str,
    max_results: int,
    page_size: int,
    api_key: Optional[str],
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    rate_limiter: SemanticScholarRateLimiter,
    request_stats: dict,
) -> List[dict]:
    candidates = []
    fetched = 0
    offset = 0
    effective_page_size = max(1, min(page_size, 100))

    while fetched < max_results:
        limit = min(effective_page_size, max_results - fetched)
        data = semantic_scholar_relevance_get(
            query=query,
            limit=limit,
            offset=offset,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            retry_sleep_base_seconds=retry_sleep_base_seconds,
            backoff_base=backoff_base,
            max_retry_sleep_seconds=max_retry_sleep_seconds,
            jitter_seconds=jitter_seconds,
            rate_limiter=rate_limiter,
            request_stats=request_stats,
        )

        papers = data.get("data") or []
        if not papers:
            break

        for paper in papers:
            candidate = map_paper_to_candidate(paper, query_family=query_family, query=query)
            if candidate:
                candidates.append(candidate)

        fetched += len(papers)

        next_offset = data.get("next")
        if next_offset in [None, ""]:
            break

        try:
            offset = int(next_offset)
        except Exception:
            offset += len(papers)

    return candidates


def search_query(
    query_family: str,
    query: str,
    endpoint: str,
    max_results: int,
    page_size: int,
    api_key: Optional[str],
    timeout: int,
    max_retries: int,
    retry_sleep_base_seconds: float,
    backoff_base: float,
    max_retry_sleep_seconds: float,
    jitter_seconds: float,
    rate_limiter: SemanticScholarRateLimiter,
    request_stats: dict,
) -> List[dict]:
    if endpoint == "bulk":
        return search_query_bulk(
            query_family=query_family,
            query=query,
            max_results=max_results,
            api_key=api_key,
            timeout=timeout,
            max_retries=max_retries,
            retry_sleep_base_seconds=retry_sleep_base_seconds,
            backoff_base=backoff_base,
            max_retry_sleep_seconds=max_retry_sleep_seconds,
            jitter_seconds=jitter_seconds,
            rate_limiter=rate_limiter,
            request_stats=request_stats,
        )

    return search_query_relevance(
        query_family=query_family,
        query=query,
        max_results=max_results,
        page_size=page_size,
        api_key=api_key,
        timeout=timeout,
        max_retries=max_retries,
        retry_sleep_base_seconds=retry_sleep_base_seconds,
        backoff_base=backoff_base,
        max_retry_sleep_seconds=max_retry_sleep_seconds,
        jitter_seconds=jitter_seconds,
        rate_limiter=rate_limiter,
        request_stats=request_stats,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search Semantic Scholar for SiC power-diode candidate papers with Zotero DOI/title-year de-duplication."
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
        "--endpoint",
        choices=["bulk", "relevance"],
        default="bulk",
        help="Semantic Scholar search endpoint. Default: bulk, recommended by Semantic Scholar for most larger retrieval tasks.",
    )

    parser.add_argument(
        "--max-results-per-query",
        type=int,
        default=100,
        help="Maximum Semantic Scholar results to inspect per query. Default: 100",
    )

    parser.add_argument(
        "--page-size",
        type=int,
        default=100,
        help="Relevance-search page size, capped at 100. Ignored by --endpoint bulk. Default: 100",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=1.25,
        help="Minimum seconds between Semantic Scholar request attempts. Default: 1.25 for a safety margin around the documented 1 request/second API-key rate.",
    )

    parser.add_argument(
        "--retry-sleep",
        type=float,
        default=2.0,
        help="Initial seconds for exponential backoff when Retry-After is absent. Default: 2.0",
    )

    parser.add_argument(
        "--retry-backoff-base",
        type=float,
        default=2.0,
        help="Exponential backoff multiplier when Retry-After is absent. Default: 2.0",
    )

    parser.add_argument(
        "--max-retry-sleep",
        type=float,
        default=32.0,
        help="Maximum exponential-backoff sleep in seconds when Retry-After is absent. Retry-After is honored even above this value. Default: 32.0",
    )

    parser.add_argument(
        "--retry-jitter",
        type=float,
        default=0.5,
        help="Random jitter, in seconds, added to non-Retry-After retry sleeps. Default: 0.5",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout in seconds. Default: 60",
    )

    parser.add_argument(
        "--max-retries",
        type=int,
        default=8,
        help="Maximum attempts for rate limits, transient Semantic Scholar errors, and request exceptions. Default: 8",
    )

    parser.add_argument(
        "--api-key-env",
        default="S2_API_KEY",
        help="Environment variable containing a Semantic Scholar API key. Default: S2_API_KEY",
    )

    parser.add_argument(
        "--skip-api-key-check",
        action="store_true",
        help="Skip the startup request that checks whether the Semantic Scholar API key appears accepted.",
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
        help="Optional maximum number of queries to run. 0 means no limit. Useful for testing.",
    )

    parser.add_argument(
        "--skip-zotero-check",
        action="store_true",
        help="Do not check Zotero for existing DOIs/title+year keys. Not recommended for production use.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    api_key = os.environ.get(args.api_key_env, "").strip()
    rate_limiter = SemanticScholarRateLimiter(min_interval_seconds=max(0.0, args.sleep))
    request_stats = {
        "request_attempt_count": 0,
        "retry_count": 0,
        "rate_limit_count": 0,
        "transient_error_count": 0,
    }

    if args.skip_api_key_check:
        api_key_check = {
            "api_key_env": args.api_key_env,
            "api_key_present": bool(api_key),
            "api_key_check_status": "skipped",
            "api_key_check_http_status": "",
            "api_key_check_message": "Semantic Scholar API key startup check skipped by --skip-api-key-check.",
        }
        print(api_key_check["api_key_check_message"])
        if api_key:
            print(f"Semantic Scholar API key presence: key found in {args.api_key_env}, but not verified.")
        else:
            print(f"Semantic Scholar API key presence: no key found in {args.api_key_env}.")
    else:
        api_key_check = check_semantic_scholar_api_key(
            api_key=api_key,
            api_key_env=args.api_key_env,
            timeout=args.timeout,
            rate_limiter=rate_limiter,
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

    print(f"Running {len(specs)} Semantic Scholar quer{'y' if len(specs) == 1 else 'ies'}.")
    print(f"Semantic Scholar endpoint: {args.endpoint}")
    print(f"Semantic Scholar request pacing: at least {max(0.0, args.sleep):g} seconds between request attempts.")
    print(
        "Semantic Scholar retry policy: "
        f"max_retries={max(1, args.max_retries)}, "
        f"retry_sleep={max(0.0, args.retry_sleep):g}, "
        f"retry_backoff_base={max(1.0, args.retry_backoff_base):g}, "
        f"max_retry_sleep={max(0.0, args.max_retry_sleep):g}; "
        "server Retry-After is honored when provided."
    )

    raw_candidates = []
    query_errors = []

    for index, (family, query) in enumerate(specs, start=1):
        print(f"[{index}/{len(specs)}] {family}: {query}")
        try:
            candidates = search_query(
                query_family=family,
                query=query,
                endpoint=args.endpoint,
                max_results=max(1, args.max_results_per_query),
                page_size=max(1, args.page_size),
                api_key=api_key,
                timeout=args.timeout,
                max_retries=max(1, args.max_retries),
                retry_sleep_base_seconds=max(0.0, args.retry_sleep),
                backoff_base=max(1.0, args.retry_backoff_base),
                max_retry_sleep_seconds=max(0.0, args.max_retry_sleep),
                jitter_seconds=max(0.0, args.retry_jitter),
                rate_limiter=rate_limiter,
                request_stats=request_stats,
            )
            raw_candidates.extend(candidates)
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
            "raw_candidate_count_before_dedupe": len(raw_candidates),
            "deduped_candidate_count_before_zotero_skip": len(deduped),
            "query_count": len(specs),
            "query_errors": query_errors,
            "zotero_collection": args.collection,
            "zotero_duplicate_check_enabled": not args.skip_zotero_check,
            "existing_zotero_doi_count": len(existing_zotero_dois),
            "existing_zotero_title_year_count": len(existing_zotero_title_years),
            "bfom_queries_enabled": not args.no_bfom_queries,
            "semantic_scholar_endpoint": args.endpoint,
            "semantic_scholar_api_key_used": bool(api_key),
            "semantic_scholar_api_key_env": args.api_key_env,
            **api_key_check,
            "semantic_scholar_sleep_seconds": max(0.0, args.sleep),
            "semantic_scholar_retry_sleep_seconds": max(0.0, args.retry_sleep),
            "semantic_scholar_retry_backoff_base": max(1.0, args.retry_backoff_base),
            "semantic_scholar_max_retry_sleep_seconds": max(0.0, args.max_retry_sleep),
            "semantic_scholar_retry_jitter_seconds": max(0.0, args.retry_jitter),
            "semantic_scholar_max_retries": max(1, args.max_retries),
            "semantic_scholar_request_attempt_count": request_stats.get("request_attempt_count", 0),
            "semantic_scholar_retry_count": request_stats.get("retry_count", 0),
            "semantic_scholar_rate_limit_count": request_stats.get("rate_limit_count", 0),
            "semantic_scholar_transient_error_count": request_stats.get("transient_error_count", 0),
        },
    )

    print("\nDone.")
    print(f"Raw candidate hits after relevance filter: {len(raw_candidates)}")
    print(f"Deduped before Zotero skip: {len(deduped)}")
    print(f"Candidates written: {len(kept)}")
    print(f"Skipped because DOI or title+year exists in Zotero: {len(skipped)}")
    print(f"Semantic Scholar request attempts: {request_stats.get('request_attempt_count', 0)}")
    print(f"Semantic Scholar retries: {request_stats.get('retry_count', 0)}")
    print(f"Semantic Scholar HTTP 429 rate limits: {request_stats.get('rate_limit_count', 0)}")
    print(f"Candidates CSV: {paths['candidates_csv']}")
    print(f"Candidates RIS: {paths['candidates_ris']}")
    print(f"Skipped audit CSV: {paths['skipped_csv']}")
    print(f"Summary JSON: {paths['summary_json']}")

    if query_errors:
        print("\nSome queries failed; see summary JSON for details.", file=sys.stderr)


if __name__ == "__main__":
    main()