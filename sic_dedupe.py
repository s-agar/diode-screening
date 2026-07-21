#!/usr/bin/env python3
"""Deduplicate DOIs against a Zotero collection tree.

This script reads newline-separated DOIs from a file, normalizes them, and
prints only those DOIs that do not already exist in the given Zotero
collection or any of its subcollections.
"""

import argparse
import sys
from pathlib import Path
from typing import List

from sic_search_common import DEFAULT_ZOTERO_COLLECTION, collect_existing_zotero_records, normalize_doi


def read_dois_from_file(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return [normalize_doi(line) for line in lines if normalize_doi(line)]


def dedupe_dois(dois: List[str]) -> List[str]:
    seen = set()
    result = []
    for doi in dois:
        if doi not in seen:
            seen.add(doi)
            result.append(doi)
    return result


def write_output(dois: List[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(dois) + ("\n" if dois else ""), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read DOIs from a file and print DOIs that do not already exist in "
            "the specified Zotero collection tree."
        )
    )
    parser.add_argument(
        "--dois-file",
        required=True,
        help="Path to newline-separated DOIs file.",
    )
    parser.add_argument(
        "--collection",
        default=DEFAULT_ZOTERO_COLLECTION,
        help=(
            "Zotero collection name, path, or key to check. "
            f"Default: {DEFAULT_ZOTERO_COLLECTION}"
        ),
    )
    parser.add_argument(
        "--output-file",
        default="",
        help="Optional path to write filtered DOIs. If omitted, writes to stdout.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    doi_file = Path(args.dois_file)

    if not doi_file.exists():
        print(f"DOI file not found: {doi_file}", file=sys.stderr)
        sys.exit(2)

    dois = read_dois_from_file(doi_file)
    unique_dois = dedupe_dois(dois)

    print(f"Reading {len(dois)} DOI entries from {doi_file}")
    print(f"  Unique normalized DOIs: {len(unique_dois)}")
    print(f"Checking Zotero collection tree: {args.collection}")

    existing_records = collect_existing_zotero_records(args.collection)
    existing_dois = set(existing_records.get("dois", {}).keys())
    print(f"  Existing Zotero DOIs: {len(existing_dois)}")

    filtered = dedupe_dois([doi for doi in unique_dois if doi not in existing_dois])

    if args.output_file:
        output_path = Path(args.output_file)
        write_output(filtered, output_path)
        print(f"Filtered DOIs written to: {output_path}")
    else:
        for doi in filtered:
            print(doi)

    print(f"DOIs not already in Zotero: {len(filtered)}")


if __name__ == "__main__":
    main()
