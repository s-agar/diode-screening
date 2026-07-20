#!/usr/bin/env python3
"""
Fetch references and citations for a list of papers via Semantic Scholar.

Input: newline-separated DOIs file (`--dois-file`) or Zotero collection `--collection`.
Outputs: CSV/RIS/skipped/summary matching other `sic_*` scripts via `write_search_outputs`.

Uses `sic_search_common.py` for DOI normalization, scoring, filtering, and Zotero duplicate checks.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import urllib.parse

import requests

from sic_search_common import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_ZOTERO_COLLECTION,
    CSV_FIELDNAMES,
    collect_existing_zotero_records,
    dedupe_candidates,
    doi_link,
    normalize_doi,
    score_candidate,
    should_keep_candidate,
    write_search_outputs,
    clean_html,
    clean_space,
)

SEMANTIC_SCHOLAR_API_BASE = "https://api.semanticscholar.org/graph/v1/paper"
SEMANTIC_SCHOLAR_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
PROVIDER_NAME = "semantic_scholar_refs"
DEFAULT_API_KEY_ENV = "S2_API_KEY"
DEFAULT_TIMEOUT = 30
DEFAULT_SLEEP = 1.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_SLEEP = 2.0
DEFAULT_RETRY_BACKOFF = 2.0
DEFAULT_MAX_RETRY_SLEEP = 60.0
DEFAULT_RETRY_JITTER = 1.0


def parse_retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if value in [None, ""]:
        return None
    try:
        return float(value)
    except Exception:
        try:
            # HTTP-date parsing not implemented; ignore
            return None
        except Exception:
            return None


def compute_wait_seconds(
    attempt: int,
    retry_after_header: Optional[str],
    base: float = DEFAULT_RETRY_SLEEP,
    backoff: float = DEFAULT_RETRY_BACKOFF,
    max_sleep: float = DEFAULT_MAX_RETRY_SLEEP,
    jitter: float = DEFAULT_RETRY_JITTER,
) -> float:
    ra = parse_retry_after_seconds(retry_after_header)
    if ra is not None:
        return max(0.0, ra)
    base_wait = max(0.0, base) * (max(1.0, backoff) ** max(0, attempt - 1))
    capped = min(max(0.0, max_sleep), base_wait)
    jitter_val = random.uniform(0.0, max(0.0, jitter)) if jitter > 0 else 0.0
    return capped + jitter_val


def semantic_scholar_get_relation(
    paper_identifier: str,
    relation: str,
    fields: str,
    api_key: Optional[str],
    timeout: int,
    max_retries: int,
    retry_sleep: float,
    backoff: float,
    max_retry_sleep: float,
    jitter: float,
    min_sleep_between_requests: float,
    max_per_paper: int,
) -> List[dict]:
    """Fetch paginated `references` or `citations` for a paper.

    `paper_identifier` should be like `DOI:10.123/...]` or a Semantic Scholar paperId.
    Returns a list of paper dicts (raw paper objects).
    """
    results: List[dict] = []
    offset = 0
    limit = 100
    headers = {"User-Agent": "sic-diode-literature-mining/1.0", "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key

    session = requests.Session()

    while True:
        params = {"fields": fields, "limit": limit, "offset": offset}
        url = f"{SEMANTIC_SCHOLAR_API_BASE}/{urllib.parse.quote(paper_identifier, safe='')}/{relation}"

        attempts_allowed = max(1, int(max_retries))
        response = None
        for attempt in range(1, attempts_allowed + 1):
            try:
                print(f"  Request attempt {attempt}/{attempts_allowed}: GET {url} params={params}")
                response = session.get(url, params=params, headers=headers, timeout=timeout)
                status = response.status_code
                print(f"  Response: HTTP {status}")
                if status == 200:
                    # success
                    break
                if status in {429, 500, 502, 503, 504}:
                    retry_after = response.headers.get("Retry-After")
                    wait = compute_wait_seconds(attempt, retry_after, retry_sleep, backoff, max_retry_sleep, jitter)
                    print(f"  Transient status {status}; retrying after {wait:.1f}s (Retry-After: {retry_after})")
                    if attempt >= attempts_allowed:
                        print(f"  Max retries reached ({attempts_allowed}) for {url}", file=sys.stderr)
                        break
                    time.sleep(wait)
                    continue
                # For other client errors like 404/400, log and give up for this paper
                try:
                    text_snip = response.text[:300]
                except Exception:
                    text_snip = ""
                print(f"  Non-retriable HTTP {status} for {url}: {text_snip}", file=sys.stderr)
                break
            except requests.RequestException as exc:
                wait = compute_wait_seconds(attempt, None, retry_sleep, backoff, max_retry_sleep, jitter)
                print(f"  Request exception: {exc}; retrying after {wait:.1f}s")
                if attempt >= attempts_allowed:
                    print(f"  Max retries reached ({attempts_allowed}) for {url} due to exceptions", file=sys.stderr)
                    response = None
                    break
                time.sleep(wait)
                continue

        if response is None:
            print(f"  Aborting fetch for {paper_identifier}/{relation}: no successful response", file=sys.stderr)
            break

        if response.status_code != 200:
            # non-success for this page/paper: already reported above, stop trying
            break

        data = response.json()
        page_items = data.get("data") or []

        # extract inner paper object depending on relation payload shape
        for entry in page_items:
            paper = None
            # references entries often have 'citedPaper' or 'reference'
            for key in ("citedPaper", "reference", "paper"):
                if isinstance(entry, dict) and key in entry and entry[key]:
                    paper = entry[key]
                    break
            # citations entries often have 'citingPaper' or 'citation'
            for key in ("citingPaper", "citation"):
                if paper is None and isinstance(entry, dict) and key in entry and entry[key]:
                    paper = entry[key]
                    break
            # fallback: some APIs may include the paper directly
            if paper is None and isinstance(entry, dict):
                # try common fields that indicate a paper
                if any(k in entry for k in ("paperId", "title", "externalIds")):
                    paper = entry

            if paper:
                results.append(paper)

        offset += len(page_items)

        if len(page_items) < limit:
            break

        if len(results) >= max_per_paper:
            break

        time.sleep(min_sleep_between_requests)

    return results[:max_per_paper]


def author_names_from_list(authors: object) -> str:
    out = []
    if isinstance(authors, list):
        for a in authors:
            if isinstance(a, dict):
                name = a.get("name") or a.get("authorName") or ""
                if name:
                    out.append(clean_space(name))
            elif isinstance(a, str):
                out.append(clean_space(a))
    return "; ".join(out)


def map_paper_to_candidate(paper: dict, query_family: str, query: str) -> Optional[dict]:
    title = clean_html(paper.get("title") or "")
    abstract = clean_html(paper.get("abstract") or "")

    if not should_keep_candidate(title, abstract):
        return None

    doi = normalize_doi((paper.get("externalIds") or {}).get("DOI") if isinstance(paper.get("externalIds"), dict) else paper.get("doi") or paper.get("DOI"))
    provider_id = clean_space(paper.get("paperId") or "")
    provider_url = clean_space(paper.get("url") or "")
    year = paper.get("year") or ""
    publication_date = paper.get("publicationDate") or ""
    venue = clean_space(paper.get("venue") or paper.get("journal") or "")
    authors = author_names_from_list(paper.get("authors") or [])

    return {
        "score": score_candidate(title, abstract),
        "database_source": "Semantic Scholar",
        "provider_id": provider_id,
        "provider_url": provider_url,
        "title": title,
        "doi": doi,
        "doi_link": doi_link(doi),
        "year": year,
        "publication_date": publication_date,
        "venue": venue,
        "authors": authors,
        "abstract": abstract,
        "url": provider_url,
        "work_type": paper.get("publicationTypes") or "",
        "is_review": False,
        "cited_by_count": paper.get("citationCount") or 0,
        "query_family": query_family,
        "query": query,
        "already_in_zotero": False,
        "skip_reason": "",
    }


def read_seed_dois_from_file(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return [normalize_doi(l) for l in lines]


def check_semantic_scholar_api_key(api_key: Optional[str], timeout: int = 10) -> bool:
    """Perform a lightweight startup check to see whether the API key appears acceptable."""
    headers = {"User-Agent": "sic-diode-literature-mining/1.0", "Accept": "application/json"}
    if api_key:
        headers["x-api-key"] = api_key

    params = {"query": "SiC diode", "fields": "paperId", "limit": 1}
    url = SEMANTIC_SCHOLAR_SEARCH_URL
    try:
        print(f"Checking Semantic Scholar API key with lightweight query: {params['query']!r}")
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        print(f"  API key check response: HTTP {resp.status_code}")
        if resp.status_code == 200:
            print("  API key check: OK")
            return True
        if resp.status_code in {401, 403}:
            print(f"  API key check: authentication error (HTTP {resp.status_code})", file=sys.stderr)
            return False
        if resp.status_code == 429:
            print("  API key check: rate limited (HTTP 429). Key may be valid but rate limit hit.")
            return True
        print(f"  API key check: unexpected status HTTP {resp.status_code}: {resp.text[:300]}")
        return False
    except requests.RequestException as exc:
        print(f"  API key check request failed: {exc}", file=sys.stderr)
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch references and citations via Semantic Scholar and output filtered candidates.")
    parser.add_argument("--dois-file", help="Path to newline-separated DOIs file.")
    parser.add_argument("--collection", default="", help=f"Zotero collection name/key for seed papers. Default: {DEFAULT_ZOTERO_COLLECTION}")
    parser.add_argument("--outdir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--api-key-env", default=DEFAULT_API_KEY_ENV)
    parser.add_argument("--api-key", default="")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--sleep", type=float, default=DEFAULT_SLEEP)
    parser.add_argument("--max-retries", type=int, default=DEFAULT_MAX_RETRIES)
    parser.add_argument("--retry-sleep", type=float, default=DEFAULT_RETRY_SLEEP)
    parser.add_argument("--retry-backoff-base", type=float, default=DEFAULT_RETRY_BACKOFF)
    parser.add_argument("--max-retry-sleep", type=float, default=DEFAULT_MAX_RETRY_SLEEP)
    parser.add_argument("--retry-jitter", type=float, default=DEFAULT_RETRY_JITTER)
    parser.add_argument("--max-per-paper", type=int, default=500, help="Maximum references+citations to fetch per seed paper")
    parser.add_argument("--limit-seeds", type=int, default=0, help="Max number of seed DOIs to process (0 = all)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = args.api_key or os.environ.get(args.api_key_env)
    if api_key:
        print(f"Semantic Scholar API key: found in {args.api_key_env}")
    else:
        print("Semantic Scholar API key: not provided; requests will be unauthenticated and rate-limited")

    # Perform quick API-key check
    key_ok = check_semantic_scholar_api_key(api_key, timeout=min(10, args.timeout))
    if key_ok:
        print("Semantic Scholar API key appears to be working (or rate-limited but valid).")
    else:
        print("Semantic Scholar API key check failed — proceeding but expect errors or limited results.", file=sys.stderr)

    seeds: List[str] = []
    if args.dois_file:
        print(f"Reading seed DOIs from file: {args.dois_file}")
        seeds = [d for d in read_seed_dois_from_file(Path(args.dois_file)) if d]
        print(f"  Seed DOIs: {len(seeds)}")
    elif args.collection:
        print(f"Checking Zotero collection: {args.collection}")
        # use Zotero collection items as seeds (only those with DOIs)
        records = collect_existing_zotero_records(args.collection)
        print(f"  Existing Zotero DOI keys: {len(records.get('dois', {}))}")
        print(f"  Existing Zotero title+year keys: {len(records.get('title_years', {}))}")
        seeds = list(records.get("dois", {}).keys())
    else:
        print("Provide --dois-file or --collection", file=sys.stderr)
        sys.exit(2)

    if args.limit_seeds > 0:
        seeds = seeds[: args.limit_seeds]

    all_candidates: List[dict] = []

    fields = ",".join([
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
        "publicationTypes",
    ])

    for idx, seed in enumerate(seeds, start=1):
        print(f"\n[{idx}/{len(seeds)}] Processing seed DOI: {seed}")
        if not seed:
            continue
        paper_id = f"DOI:{seed}"
        refs = semantic_scholar_get_relation(
            paper_id,
            "references",
            fields,
            api_key,
            args.timeout,
            args.max_retries,
            args.retry_sleep,
            args.retry_backoff_base,
            args.max_retry_sleep,
            args.retry_jitter,
            args.sleep,
            args.max_per_paper,
        )

        cits = semantic_scholar_get_relation(
            paper_id,
            "citations",
            fields,
            api_key,
            args.timeout,
            args.max_retries,
            args.retry_sleep,
            args.retry_backoff_base,
            args.max_retry_sleep,
            args.retry_jitter,
            args.sleep,
            args.max_per_paper,
        )

        print(f"  References fetched: {len(refs)}")
        for p in refs:
            cand = map_paper_to_candidate(p, "reference", seed)
            if cand:
                all_candidates.append(cand)
        print(f"  Citations fetched: {len(cits)}")
        for p in cits:
            cand = map_paper_to_candidate(p, "citation", seed)
            if cand:
                all_candidates.append(cand)

    # dedupe and mark Zotero duplicates using default collection if available
    deduped = dedupe_candidates(all_candidates)
    existing = collect_existing_zotero_records(DEFAULT_ZOTERO_COLLECTION)
    kept, skipped = [], []
    kept, skipped = __import__("sic_search_common").mark_zotero_duplicates(deduped, existing.get("dois", {}), existing.get("title_years", {}))

    outdir = Path(args.outdir)
    paths = write_search_outputs(kept, skipped, outdir, PROVIDER_NAME, summary_extra={"seed_count": len(seeds)})

    print("\nSummary:")
    print(f"  Seed papers processed: {len(seeds)}")
    print(f"  Candidates kept: {len(kept)}")
    print(f"  Candidates skipped (Zotero duplicates): {len(skipped)}")
    print(f"  Candidates CSV: {paths.get('candidates_csv')}")
    print(f"  Candidates RIS: {paths.get('candidates_ris')}")
    print(f"  Skipped CSV: {paths.get('skipped_csv')}")
    print(f"  Summary JSON: {paths.get('summary_json')}")


if __name__ == "__main__":
    main()
