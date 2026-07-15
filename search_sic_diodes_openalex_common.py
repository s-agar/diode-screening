#!/usr/bin/env python3
"""
Search OpenAlex for SiC power-diode candidate papers using the shared
Plan-C search helper. Version v4 expects sic_search_common.py with collect_existing_zotero_records().

This script is append-safe in the sense that it never overwrites prior search
outputs. Each run writes timestamped CSV/RIS/audit/summary files. It also checks
Zotero's "SiC Screening" collection tree by DOI and by conservative normalized
title+year before writing candidate outputs, so already-screened Zotero records
are excluded from candidates and written to a skipped audit CSV instead.

Expected companion file in the same directory:
    sic_search_common.py
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from sic_search_common import (
    DEFAULT_OUTPUT_DIR,
    DEFAULT_ZOTERO_COLLECTION,
    clean_html,
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


OPENALEX_WORKS_URL = "https://api.openalex.org/works"
DEFAULT_PROVIDER_NAME = "openalex"
DEFAULT_MAX_RESULTS_PER_QUERY = 100
DEFAULT_SLEEP_SECONDS = 1.0


OPENALEX_SELECT_FIELDS = [
    "id",
    "doi",
    "title",
    "display_name",
    "publication_year",
    "publication_date",
    "authorships",
    "primary_location",
    "locations",
    "open_access",
    "abstract_inverted_index",
    "cited_by_count",
    "type",
]


def inverted_index_to_text(inv: object) -> str:
    """Convert OpenAlex abstract_inverted_index to readable text."""
    if not isinstance(inv, dict):
        return ""

    positions = []
    for word, idxs in inv.items():
        if not isinstance(idxs, list):
            continue
        for idx in idxs:
            try:
                positions.append((int(idx), str(word)))
            except Exception:
                continue

    if not positions:
        return ""

    words = [word for _, word in sorted(positions)]
    return " ".join(words)


def extract_authors(work: dict) -> str:
    authors = []
    for authorship in work.get("authorships") or []:
        author = authorship.get("author") or {}
        name = author.get("display_name") or ""
        if name:
            authors.append(name)
    return "; ".join(authors)


def extract_venue(work: dict) -> str:
    loc = work.get("primary_location") or {}
    source = loc.get("source") or {}
    return source.get("display_name") or ""


def extract_best_url(work: dict) -> str:
    """Prefer OA URL, then PDF URL, then landing page URL."""
    oa = work.get("open_access") or {}
    best = oa.get("oa_url") or ""

    pdf_candidates = []
    landing_candidates = []

    for loc in work.get("locations") or []:
        if not isinstance(loc, dict):
            continue
        pdf_url = loc.get("pdf_url") or ""
        landing_url = loc.get("landing_page_url") or ""
        if pdf_url:
            pdf_candidates.append(pdf_url)
        if landing_url:
            landing_candidates.append(landing_url)

    if best:
        return best
    if pdf_candidates:
        return pdf_candidates[0]
    if landing_candidates:
        return landing_candidates[0]
    return ""


def openalex_search(
    query: str,
    per_page: int,
    page: int,
    email: str = "",
    timeout: int = 60,
) -> List[dict]:
    params = {
        "search": query,
        "per-page": str(per_page),
        "page": str(page),
        "select": ",".join(OPENALEX_SELECT_FIELDS),
    }

    if email:
        params["mailto"] = email

    url = OPENALEX_WORKS_URL + "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"SiC diode literature screening ({email})" if email else "SiC diode literature screening",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))

    results = data.get("results") or []
    if not isinstance(results, list):
        return []
    return results


def work_to_candidate(work: dict, query_family: str, query: str) -> dict:
    title = clean_html(work.get("title") or work.get("display_name") or "")
    abstract = clean_html(inverted_index_to_text(work.get("abstract_inverted_index")))
    doi = normalize_doi(work.get("doi") or "")
    year = work.get("publication_year") or ""
    publication_date = work.get("publication_date") or ""
    work_type = work.get("type") or ""
    openalex_id = work.get("id") or ""
    url = extract_best_url(work)

    return {
        "score": score_candidate(title, abstract),
        "database_source": "openalex",
        "provider_id": openalex_id,
        "provider_url": openalex_id,
        "title": title,
        "doi": doi,
        "doi_link": doi_link(doi),
        "year": year,
        "publication_date": publication_date,
        "venue": extract_venue(work),
        "authors": extract_authors(work),
        "abstract": abstract,
        "url": url,
        "work_type": work_type,
        "is_review": infer_is_review(work_type, title, abstract),
        "cited_by_count": work.get("cited_by_count") or 0,
        "query_family": query_family,
        "query": query,
        "already_in_zotero": False,
        "skip_reason": "",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search OpenAlex for SiC power-diode candidate papers with Zotero duplicate filtering."
    )

    parser.add_argument(
        "--outdir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )

    parser.add_argument(
        "--zotero-collection",
        default=DEFAULT_ZOTERO_COLLECTION,
        help=f"Zotero collection name/key/path used for duplicate filtering. Default: {DEFAULT_ZOTERO_COLLECTION}",
    )

    parser.add_argument(
        "--no-zotero-check",
        action="store_true",
        help="Do not query Zotero and do not skip results already present in Zotero.",
    )

    parser.add_argument(
        "--email",
        default=os.environ.get("OPENALEX_EMAIL", ""),
        help="Email for OpenAlex polite-pool mailto parameter. Can also use OPENALEX_EMAIL environment variable.",
    )

    parser.add_argument(
        "--max-results-per-query",
        type=int,
        default=DEFAULT_MAX_RESULTS_PER_QUERY,
        help=f"OpenAlex per-page result count per query. Default: {DEFAULT_MAX_RESULTS_PER_QUERY}",
    )

    parser.add_argument(
        "--pages-per-query",
        type=int,
        default=1,
        help="Number of OpenAlex result pages to fetch per query. Default: 1",
    )

    parser.add_argument(
        "--limit-queries",
        type=int,
        default=0,
        help="Optional maximum number of query strings to run. 0 means all queries.",
    )

    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP_SECONDS,
        help=f"Seconds to sleep between OpenAlex API requests. Default: {DEFAULT_SLEEP_SECONDS}",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="HTTP timeout per OpenAlex request in seconds. Default: 60",
    )

    parser.add_argument(
        "--no-bfom-queries",
        action="store_true",
        help="Disable BFOM/FOM/Baliga figure-of-merit query variants.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    specs = query_specs(include_bfom=not args.no_bfom_queries)
    if args.limit_queries and args.limit_queries > 0:
        specs = specs[:args.limit_queries]

    existing_zotero_records = {"dois": {}, "title_years": {}}
    if not args.no_zotero_check:
        print(f"Checking Zotero collection tree: {args.zotero_collection}")
        existing_zotero_records = collect_existing_zotero_records(args.zotero_collection)
        print(f"  Existing Zotero DOI keys: {len(existing_zotero_records.get('dois', {}))}")
        print(f"  Existing Zotero title+year keys: {len(existing_zotero_records.get('title_years', {}))}")
    else:
        print("Skipping Zotero duplicate check.")

    raw_candidates = []
    request_count = 0
    query_errors = []

    per_page = max(1, min(int(args.max_results_per_query), 200))
    pages_per_query = max(1, int(args.pages_per_query))

    for q_index, (query_family, query) in enumerate(specs, start=1):
        print(f"\n[{q_index}/{len(specs)}] Searching OpenAlex: {query}")

        for page in range(1, pages_per_query + 1):
            try:
                works = openalex_search(
                    query=query,
                    per_page=per_page,
                    page=page,
                    email=args.email,
                    timeout=args.timeout,
                )
                request_count += 1
            except Exception as exc:
                message = f"query={query!r}; page={page}; error={exc}"
                print(f"  ERROR: {message}", file=sys.stderr)
                query_errors.append(message)
                break

            print(f"  Page {page}: {len(works)} works returned")

            if not works:
                break

            kept_from_page = 0
            for work in works:
                candidate = work_to_candidate(work, query_family=query_family, query=query)
                if not should_keep_candidate(candidate.get("title"), candidate.get("abstract")):
                    continue
                raw_candidates.append(candidate)
                kept_from_page += 1

            print(f"  Page {page}: {kept_from_page} passed initial title/abstract filter")

            if page < pages_per_query and args.sleep > 0:
                time.sleep(args.sleep)

        if q_index < len(specs) and args.sleep > 0:
            time.sleep(args.sleep)

    deduped = dedupe_candidates(raw_candidates)
    kept, skipped = mark_zotero_duplicates(
        deduped,
        existing_zotero_records.get("dois", {}),
        existing_zotero_records.get("title_years", {}),
    )

    paths = write_search_outputs(
        candidates=kept,
        skipped=skipped,
        outdir=outdir,
        provider_name=DEFAULT_PROVIDER_NAME,
        summary_extra={
            "provider": "OpenAlex",
            "request_count": request_count,
            "query_count_requested": len(specs),
            "raw_candidate_count_before_internal_dedupe": len(raw_candidates),
            "deduped_candidate_count_before_zotero_filter": len(deduped),
            "existing_zotero_doi_count": len(existing_zotero_records.get("dois", {})),
            "existing_zotero_title_year_count": len(existing_zotero_records.get("title_years", {})),
            "zotero_duplicate_filter_enabled": not args.no_zotero_check,
            "zotero_collection": args.zotero_collection,
            "include_bfom_queries": not args.no_bfom_queries,
            "max_results_per_query": per_page,
            "pages_per_query": pages_per_query,
            "query_errors": query_errors,
        },
    )

    print("\nDone.")
    print(f"Raw candidates before internal dedupe: {len(raw_candidates)}")
    print(f"Deduped candidates before Zotero filter: {len(deduped)}")
    print(f"Candidates written: {len(kept)}")
    print(f"Skipped existing in Zotero: {len(skipped)}")
    print(f"CSV: {paths['candidates_csv']}")
    print(f"RIS: {paths['candidates_ris']}")
    print(f"Skipped audit CSV: {paths['skipped_csv']}")
    print(f"Summary JSON: {paths['summary_json']}")


if __name__ == "__main__":
    main()
