#!/usr/bin/env python3
"""
Shared helpers for SiC power-diode literature search scripts.

This module centralizes:
- Zotero Local API duplicate checking against a collection and subcollections
- DOI normalization
- candidate scoring / filtering
- timestamped CSV, RIS, and skipped-result audit output

Provider-specific scripts should map their API responses into the common
candidate dictionary fields used here.
"""

import csv
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests


ZOTERO_BASE_URL = "http://localhost:23119/api"
DEFAULT_ZOTERO_COLLECTION = "SiC Screening"
SEARCH_COMMON_VERSION = "2026-07-10-v4-title-year-zotero-records"
DEFAULT_OUTPUT_DIR = "sic_diode_search_results"


SIC_TITLE_PATTERNS = [
    r"\bsic\b",
    r"\b4h[- ]?sic\b",
    r"\b6h[- ]?sic\b",
    r"\bsilicon carbide\b",
]


DIODE_TERMS = [
    "diode",
    "diodes",
    "schottky",
    "sbd",
    "sbds",
    "jbs diode",
    "jbs diodes",
    "junction barrier schottky",
    "pin diode",
    "pin diodes",
    "p-i-n diode",
    "p-i-n diodes",
    "pn diode",
    "pn diodes",
    "p-n diode",
    "p-n diodes",
    "mps diode",
    "mps diodes",
    "rectifier",
    "rectifiers",
]


EVIDENCE_TERMS = [
    "specific on-resistance",
    "specific on resistance",
    "specific differential on-resistance",
    "specific differential on resistance",
    "differential specific on-resistance",
    "differential specific on resistance",
    "on-resistance",
    "on resistance",
    "ron",
    "ron,sp",
    "r_on",
    "r_on,sp",
    "ronsp",
    "breakdown",
    "breakdown voltage",
    "blocking voltage",
    "reverse blocking voltage",
    "bv",
    "vbr",
    "edge termination",
    "edge-termination",
    "peripheral termination",
    "termination structure",
    "termination region",
    "junction termination extension",
    "junction-termination extension",
    "jte",
    "field limiting ring",
    "field-limiting ring",
    "floating field ring",
    "floating-field ring",
    "flr",
    "ffr",
    "guard ring",
    "guard-ring",
    "field plate",
    "field-plate",
    "mesa",
    "mesa termination",
    "mesa edge termination",
    "etched mesa",
    "bevel",
    "beveled",
    "bevelled",
    "bevel termination",
    "beveled mesa",
    "bevelled mesa",
    "negative bevel",
    "positive bevel",
    "baliga figure of merit",
    "baliga's figure of merit",
    "baliga figure-of-merit",
    "baliga's figure-of-merit",
    "unipolar figure of merit",
    "unipolar figure-of-merit",
    "figure of merit",
    "figure-of-merit",
    "bfom",
    "fom",
    "bv2/ron",
    "bv2/ronsp",
    "bv2/ron,sp",
    "bv^2/ron",
    "bv^2/ronsp",
    "bv^2/ron,sp",
]


REVIEW_PHRASES_TITLE = [
    "review",
    "a review",
    "review of",
    "recent progress",
    "overview",
    "perspective",
]


TRANSISTOR_FALSE_POSITIVE_TERMS = [
    "mosfet",
    "jfet",
    "mesfet",
    "hemt",
    "fet",
    "jbsfet",
    "vdmosfet",
    "misfet",
    "finfet",
    "igbt",
    "igtbt",
    "vjfet",
    "bidfet",
    "dmosfet",
    "bjt",
    "photodiode",
    "led",
    "gan",
    "ga2o3",
    "gallium oxide",
]


BASE_QUERY_SPECS = [
    ("core", "4H SiC diode specific on resistance breakdown voltage"),
    ("core", "SiC Schottky barrier diode specific on resistance breakdown voltage"),
    ("core", "SiC JBS diode specific on resistance breakdown voltage"),
    ("core", "SiC PiN diode specific on resistance breakdown voltage"),
    ("core", "SiC PIN diode specific on resistance breakdown voltage"),
    ("core", "SiC PN diode specific on resistance breakdown voltage"),
    ("core", "SiC p n diode specific on resistance breakdown voltage"),
    ("core", "SiC p i n diode specific on resistance breakdown voltage"),
    ("core", "SiC MPS diode specific on resistance breakdown voltage"),
    ("edge", "SiC diode edge termination breakdown voltage"),
    ("edge", "SiC diode junction termination extension specific on resistance"),
    ("edge", "SiC diode JTE specific on resistance breakdown voltage"),
    ("edge", "SiC diode field limiting rings breakdown voltage"),
    ("edge", "SiC diode floating field rings breakdown voltage"),
    ("edge", "SiC diode guard ring specific on resistance"),
    ("edge", "SiC diode field plate breakdown voltage"),
    ("edge", "SiC diode mesa termination breakdown voltage"),
    ("edge", "SiC diode mesa edge termination specific on resistance"),
    ("edge", "SiC diode etched mesa breakdown voltage"),
    ("edge", "SiC diode bevel termination breakdown voltage"),
    ("edge", "SiC diode beveled mesa breakdown voltage"),
    ("edge", "SiC diode bevel edge termination specific on resistance"),
    ("edge", "SiC diode beveled mesa termination specific on resistance"),
]


BFOM_QUERY_SPECS = [
    ("bfom", "4H SiC diode Baliga figure of merit"),
    ("bfom", "4H SiC diode Baliga's figure of merit"),
    ("bfom", "SiC diode Baliga figure of merit"),
    ("bfom", "SiC diode Baliga's figure of merit"),
    ("bfom", "SiC Schottky diode Baliga figure of merit"),
    ("bfom", "SiC JBS diode Baliga figure of merit"),
    ("bfom", "SiC PiN diode Baliga figure of merit"),
    ("bfom", "SiC PIN diode Baliga figure of merit"),
    ("bfom", "SiC rectifier Baliga figure of merit"),
    ("bfom", "SiC diode BFOM breakdown voltage specific on resistance"),
    ("bfom", "SiC Schottky diode BFOM breakdown voltage specific on resistance"),
    ("bfom", "SiC JBS diode BFOM breakdown voltage specific on resistance"),
    ("bfom", "SiC PiN diode BFOM breakdown voltage specific on resistance"),
    ("bfom", "SiC diode figure of merit breakdown voltage specific on resistance"),
    ("bfom", "SiC diode FOM breakdown voltage specific on resistance"),
    ("bfom", "SiC diode unipolar figure of merit"),
    ("bfom", "SiC diode unipolar FOM"),
    ("bfom", "SiC diode unipolar Baliga figure of merit"),
    ("bfom", "SiC diode BV2 Ronsp"),
    ("bfom", "SiC diode BV squared Ronsp"),
    ("bfom", "SiC diode BV2 Ron sp"),
    ("bfom", "SiC diode BV squared Ron sp"),
    ("bfom", "SiC diode BV2 over Ronsp"),
    ("bfom", "SiC diode BV squared over Ronsp"),
    ("bfom", "SiC diode breakdown voltage squared specific on resistance"),
    ("bfom", "SiC diode breakdown voltage on resistance figure of merit"),
]


CSV_FIELDNAMES = [
    "score",
    "database_source",
    "provider_id",
    "provider_url",
    "title",
    "doi",
    "doi_link",
    "year",
    "publication_date",
    "venue",
    "authors",
    "abstract",
    "url",
    "work_type",
    "is_review",
    "cited_by_count",
    "query_family",
    "query",
    "already_in_zotero",
    "skip_reason",
]


SKIPPED_FIELDNAMES = CSV_FIELDNAMES + [
    "matched_zotero_duplicate_type",
    "matched_zotero_duplicate_key",
    "matched_zotero_collection_key",
    "matched_zotero_collection_path",
    "matched_zotero_item_key",
    "matched_zotero_title",
    "matched_zotero_year",
]


def clean_html(text: object) -> str:
    if text is None:
        return ""
    text = str(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_space(text: object) -> str:
    return re.sub(r"\s+", " ", "" if text is None else str(text)).strip()


def normalize_doi(doi: object) -> str:
    if doi is None:
        return ""

    doi_text = clean_space(doi)
    doi_text = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", doi_text)
    doi_text = re.sub(r"(?i)^doi:\s*", "", doi_text)
    doi_text = doi_text.strip().strip(".;,)")
    return doi_text.lower()


def doi_link(doi: object) -> str:
    normalized = normalize_doi(doi)
    if not normalized:
        return ""
    return f"https://doi.org/{normalized}"


def normalize_title_for_dedupe(title: object) -> str:
    """Normalize titles conservatively for duplicate matching."""
    text = clean_html(title).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_year(year: object) -> str:
    text = clean_space(year)
    match = re.search(r"\b((?:19|20)\d{2})\b", text)
    return match.group(1) if match else ""


def title_year_key(title: object, year: object) -> str:
    normalized_title = normalize_title_for_dedupe(title)
    normalized_year = normalize_year(year)
    if not normalized_title or not normalized_year:
        return ""
    return f"{normalized_title}::{normalized_year}"


def title_has_sic(title: object) -> bool:
    title_l = clean_space(title).lower()
    return any(re.search(pattern, title_l) for pattern in SIC_TITLE_PATTERNS)


def infer_is_review(work_type: object, title: object, abstract: object) -> bool:
    work_type_l = clean_space(work_type).lower()
    title_l = clean_space(title).lower()
    abstract_l = clean_space(abstract).lower()

    if work_type_l == "review":
        return True

    if any(phrase in title_l for phrase in REVIEW_PHRASES_TITLE):
        return True

    if any(phrase in abstract_l[:500] for phrase in [
        "this review",
        "we review",
        "this paper reviews",
        "this article reviews",
    ]):
        return True

    return False


def should_keep_candidate(title: object, abstract: object) -> bool:
    text_l = f"{clean_space(title)} {clean_space(abstract)}".lower()

    if not title_has_sic(title):
        return False

    return any(term in text_l for term in DIODE_TERMS)


def score_candidate(title: object, abstract: object) -> int:
    title_l = clean_space(title).lower()
    text_l = f"{clean_space(title)} {clean_space(abstract)}".lower()

    score = 0

    if title_has_sic(title):
        score += 5

    if any(term in title_l for term in DIODE_TERMS):
        score += 5
    elif any(term in text_l for term in DIODE_TERMS):
        score += 3

    for term in EVIDENCE_TERMS:
        if term in text_l:
            score += 1

    for bad in TRANSISTOR_FALSE_POSITIVE_TERMS:
        if bad in text_l:
            score -= 1

    return score


def query_specs(include_bfom: bool = True) -> List[Tuple[str, str]]:
    specs = list(BASE_QUERY_SPECS)
    if include_bfom:
        specs.extend(BFOM_QUERY_SPECS)

    seen = set()
    deduped = []
    for family, query in specs:
        key = query.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append((family, query))
    return deduped


def timestamp_string() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def zotero_get_json(endpoint: str, params: Optional[dict] = None, timeout: int = 60) -> object:
    url = f"{ZOTERO_BASE_URL}{endpoint}"
    response = requests.get(url, params=params, timeout=timeout)

    if response.status_code == 403:
        raise RuntimeError(
            "Zotero Local API returned 403. Enable Zotero setting: "
            "Settings -> Advanced -> Allow other applications on this computer to communicate with Zotero."
        )

    response.raise_for_status()
    return response.json()


def check_zotero() -> None:
    response = requests.get(f"{ZOTERO_BASE_URL}/users/0/items", params={"limit": 1}, timeout=20)
    if response.status_code == 403:
        raise RuntimeError(
            "Zotero Local API is not enabled. In Zotero, enable: "
            "Settings -> Advanced -> Allow other applications on this computer to communicate with Zotero."
        )
    response.raise_for_status()


def paginated_zotero_get(endpoint: str, params: Optional[dict] = None, limit: int = 100) -> List[dict]:
    params_base = dict(params or {})
    params_base["limit"] = limit

    records = []
    start = 0

    while True:
        params_page = dict(params_base)
        params_page["start"] = start
        page = zotero_get_json(endpoint, params=params_page)

        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected Zotero response for {endpoint}")

        records.extend(page)

        if len(page) < limit:
            break

        start += limit

    return records


def get_all_collections() -> List[dict]:
    return paginated_zotero_get("/users/0/collections")


def collection_data(collection: dict) -> dict:
    return collection.get("data") or {}


def collection_key(collection: dict) -> str:
    return collection.get("key") or collection_data(collection).get("key") or ""


def collection_name(collection: dict) -> str:
    return collection_data(collection).get("name") or ""


def parent_collection_key(collection: dict) -> str:
    return collection_data(collection).get("parentCollection") or ""


def build_collection_paths(collections: Sequence[dict]) -> Dict[str, str]:
    by_key = {collection_key(c): c for c in collections if collection_key(c)}

    def path_for(key: str, seen: Optional[set] = None) -> str:
        if seen is None:
            seen = set()
        if key in seen:
            return collection_name(by_key.get(key, {})) or key
        seen.add(key)

        collection = by_key.get(key)
        if not collection:
            return key

        name = collection_name(collection) or key
        parent = parent_collection_key(collection)

        if parent and parent in by_key:
            return f"{path_for(parent, seen)} / {name}"
        return name

    return {key: path_for(key) for key in by_key}


def find_collection(collection_name_or_key: str, collections: Sequence[dict]) -> Tuple[str, str]:
    paths = build_collection_paths(collections)

    exact_key_matches = [
        (collection_key(c), paths.get(collection_key(c), collection_name(c)))
        for c in collections
        if collection_key(c) == collection_name_or_key
    ]
    if exact_key_matches:
        return exact_key_matches[0]

    exact_name_matches = [
        (collection_key(c), paths.get(collection_key(c), collection_name(c)))
        for c in collections
        if collection_name(c) == collection_name_or_key
    ]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]
    if len(exact_name_matches) > 1:
        options = "\n".join(f"  {key}: {path}" for key, path in exact_name_matches)
        raise RuntimeError(
            f"Multiple collections are named {collection_name_or_key!r}. Use the collection key.\n{options}"
        )

    path_matches = [(key, path) for key, path in paths.items() if path == collection_name_or_key]
    if len(path_matches) == 1:
        return path_matches[0]

    contains_matches = [
        (key, path)
        for key, path in paths.items()
        if collection_name_or_key.lower() in path.lower()
    ]
    if len(contains_matches) == 1:
        return contains_matches[0]

    if contains_matches:
        options = "\n".join(f"  {key}: {path}" for key, path in contains_matches)
        raise RuntimeError(
            f"Could not uniquely identify collection {collection_name_or_key!r}. Possible matches:\n{options}"
        )

    available = "\n".join(
        f"  {key}: {path}"
        for key, path in sorted(paths.items(), key=lambda kv: kv[1].lower())
    )
    raise RuntimeError(f"Collection not found: {collection_name_or_key!r}. Available collections:\n{available}")


def descendant_collection_keys(root_key: str, collections: Sequence[dict]) -> List[str]:
    children_by_parent: Dict[str, List[str]] = {}
    for collection in collections:
        key = collection_key(collection)
        parent = parent_collection_key(collection)
        if not key:
            continue
        children_by_parent.setdefault(parent, []).append(key)

    result = []
    stack = [root_key]
    seen = set()

    while stack:
        key = stack.pop()
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
        stack.extend(reversed(children_by_parent.get(key, [])))

    return result


def extract_doi_from_zotero_item(item: dict) -> str:
    data = item.get("data") or {}
    doi = data.get("DOI") or data.get("doi") or ""
    if doi:
        return normalize_doi(doi)

    extra = data.get("extra") or data.get("Extra") or ""
    match = re.search(r"(?im)^\s*DOI\s*:\s*(.+?)\s*$", extra)
    if match:
        return normalize_doi(match.group(1))

    # Conservative fallback for Extra fields containing a DOI URL.
    match = re.search(r"(?i)https?://(?:dx\.)?doi\.org/([^\s]+)", extra)
    if match:
        return normalize_doi(match.group(1))

    return ""


def extract_year_from_zotero_item(item: dict) -> str:
    data = item.get("data") or {}
    for key in ["date", "year", "publicationDate"]:
        year = normalize_year(data.get(key) or "")
        if year:
            return year
    return ""


def collect_existing_zotero_records(collection_name_or_key: str = DEFAULT_ZOTERO_COLLECTION) -> Dict[str, Dict[str, dict]]:
    """Collect Zotero duplicate keys from a collection tree.

    DOI matches are preferred. A conservative normalized-title-plus-year key is
    also collected so search scripts can skip obvious duplicates where DOI is
    missing from either the provider result or the Zotero item.
    """
    check_zotero()
    collections = get_all_collections()
    paths = build_collection_paths(collections)
    root_key, root_path = find_collection(collection_name_or_key, collections)
    collection_keys = descendant_collection_keys(root_key, collections)

    doi_records: Dict[str, dict] = {}
    title_year_records: Dict[str, dict] = {}

    for key in collection_keys:
        items = paginated_zotero_get(f"/users/0/collections/{key}/items")
        for item in items:
            data = item.get("data") or {}
            if data.get("itemType") == "attachment":
                continue

            title = data.get("title") or ""
            year = extract_year_from_zotero_item(item)
            doi = extract_doi_from_zotero_item(item)

            base_record = {
                "doi": doi,
                "title_year_key": title_year_key(title, year),
                "year": year,
                "zotero_item_key": item.get("key") or data.get("key") or "",
                "zotero_title": title,
                "collection_key": key,
                "collection_path": paths.get(key, key),
                "root_collection_key": root_key,
                "root_collection_path": root_path,
            }

            if doi:
                doi_records.setdefault(doi, dict(base_record))

            ty_key = base_record["title_year_key"]
            if ty_key:
                title_year_records.setdefault(ty_key, dict(base_record))

    return {
        "dois": doi_records,
        "title_years": title_year_records,
    }


def collect_existing_zotero_dois(collection_name_or_key: str = DEFAULT_ZOTERO_COLLECTION) -> Dict[str, dict]:
    """Backward-compatible helper returning only Zotero DOI records."""
    return collect_existing_zotero_records(collection_name_or_key).get("dois", {})



def merge_candidate(existing: dict, incoming: dict) -> dict:
    merged = dict(existing)

    existing_score = int(existing.get("score") or 0)
    incoming_score = int(incoming.get("score") or 0)
    if incoming_score > existing_score:
        for key in [
            "score",
            "title",
            "doi",
            "doi_link",
            "year",
            "publication_date",
            "venue",
            "authors",
            "abstract",
            "url",
            "provider_url",
            "work_type",
            "is_review",
            "cited_by_count",
        ]:
            if incoming.get(key) not in [None, ""]:
                merged[key] = incoming.get(key)

    for key in ["query", "query_family", "database_source", "provider_id"]:
        old_values = [v.strip() for v in str(merged.get(key, "")).split(" | ") if v.strip()]
        new_values = [v.strip() for v in str(incoming.get(key, "")).split(" | ") if v.strip()]
        values = []
        for value in old_values + new_values:
            if value not in values:
                values.append(value)
        merged[key] = " | ".join(values)

    try:
        merged["cited_by_count"] = max(int(existing.get("cited_by_count") or 0), int(incoming.get("cited_by_count") or 0))
    except Exception:
        pass

    merged["already_in_zotero"] = existing.get("already_in_zotero") or incoming.get("already_in_zotero") or False
    merged["skip_reason"] = existing.get("skip_reason") or incoming.get("skip_reason") or ""

    return merged


def dedupe_candidates(candidates: Iterable[dict]) -> List[dict]:
    seen: Dict[str, dict] = {}

    for candidate in candidates:
        doi = normalize_doi(candidate.get("doi"))
        title = normalize_title_for_dedupe(candidate.get("title"))
        year = normalize_year(candidate.get("year") or candidate.get("publication_date"))
        ty_key = title_year_key(title, year)

        if doi:
            key = f"doi:{doi}"
        elif ty_key:
            key = f"title_year:{ty_key}"
        elif title:
            key = f"title:{title}"
        else:
            continue

        if key in seen:
            seen[key] = merge_candidate(seen[key], candidate)
        else:
            row = dict(candidate)
            row["doi"] = doi
            row["doi_link"] = doi_link(doi)
            seen[key] = row

    return list(seen.values())



def sort_candidates(candidates: Sequence[dict]) -> List[dict]:
    return sorted(
        candidates,
        key=lambda r: (
            bool(r.get("is_review", False)),
            -int(r.get("score") or 0),
            -int(r.get("cited_by_count") or 0),
            str(r.get("year") or ""),
            str(r.get("title") or "").lower(),
        ),
    )


def make_ris_record(row: dict) -> str:
    lines = []
    lines.append("TY  - JOUR")

    if row.get("title"):
        lines.append(f"TI  - {row['title']}")

    for author in str(row.get("authors") or "").split("; "):
        if author:
            lines.append(f"AU  - {author}")

    if row.get("year"):
        lines.append(f"PY  - {row['year']}")

    if row.get("venue"):
        lines.append(f"JO  - {row['venue']}")

    if row.get("doi"):
        lines.append(f"DO  - {row['doi']}")

    if row.get("url"):
        lines.append(f"UR  - {row['url']}")

    if row.get("abstract"):
        lines.append(f"AB  - {row['abstract']}")

    lines.append(
        "N1  - "
        f"database_source: {row.get('database_source', '')}; "
        f"provider_id: {row.get('provider_id', '')}; "
        f"provider_url: {row.get('provider_url', '')}; "
        f"query_family: {row.get('query_family', '')}; "
        f"search_query: {row.get('query', '')}; "
        f"initial_score: {row.get('score', '')}; "
        f"work_type: {row.get('work_type', '')}; "
        f"is_review: {row.get('is_review', '')}"
    )

    lines.append("ER  -")
    return "\n".join(lines)


def write_csv(path: Path, rows: Sequence[dict], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_ris(path: Path, rows: Sequence[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(make_ris_record(row))
            f.write("\n\n")


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def output_paths(outdir: Path, provider_name: str, stamp: Optional[str] = None) -> Dict[str, Path]:
    if stamp is None:
        stamp = timestamp_string()
    safe_provider = re.sub(r"[^A-Za-z0-9_.-]+", "_", provider_name).strip("_") or "search"
    prefix = f"{safe_provider}_{stamp}"
    return {
        "timestamp": stamp,
        "candidates_csv": outdir / f"{prefix}_candidates.csv",
        "candidates_ris": outdir / f"{prefix}_candidates.ris",
        "skipped_csv": outdir / f"{prefix}_skipped_existing_in_zotero.csv",
        "summary_json": outdir / f"{prefix}_summary.json",
    }


def write_search_outputs(
    candidates: Sequence[dict],
    skipped: Sequence[dict],
    outdir: Path,
    provider_name: str,
    summary_extra: Optional[dict] = None,
) -> Dict[str, Path]:
    paths = output_paths(outdir, provider_name)
    sorted_rows = sort_candidates(candidates)
    skipped_rows = sort_candidates(skipped)

    write_csv(paths["candidates_csv"], sorted_rows, CSV_FIELDNAMES)
    write_ris(paths["candidates_ris"], sorted_rows)
    write_csv(paths["skipped_csv"], skipped_rows, SKIPPED_FIELDNAMES)

    summary = {
        "provider_name": provider_name,
        "timestamp": paths["timestamp"],
        "candidate_count": len(sorted_rows),
        "skipped_existing_in_zotero_count": len(skipped_rows),
        "candidates_csv": str(paths["candidates_csv"]),
        "candidates_ris": str(paths["candidates_ris"]),
        "skipped_csv": str(paths["skipped_csv"]),
    }
    if summary_extra:
        summary.update(summary_extra)
    write_json(paths["summary_json"], summary)

    return paths


def mark_zotero_duplicates(
    candidates: Sequence[dict],
    existing_zotero_dois: Dict[str, dict],
    existing_zotero_title_years: Optional[Dict[str, dict]] = None,
) -> Tuple[List[dict], List[dict]]:
    kept = []
    skipped = []
    existing_zotero_title_years = existing_zotero_title_years or {}

    for candidate in candidates:
        row = dict(candidate)
        doi = normalize_doi(row.get("doi"))
        row["doi"] = doi
        row["doi_link"] = doi_link(doi)

        match_type = ""
        match_key = ""
        zotero_record = None

        if doi and doi in existing_zotero_dois:
            match_type = "doi"
            match_key = doi
            zotero_record = existing_zotero_dois[doi]
        else:
            ty_key = title_year_key(row.get("title"), row.get("year") or row.get("publication_date"))
            if ty_key and ty_key in existing_zotero_title_years:
                match_type = "normalized_title_year"
                match_key = ty_key
                zotero_record = existing_zotero_title_years[ty_key]

        if zotero_record:
            row["already_in_zotero"] = True
            if match_type == "doi":
                row["skip_reason"] = "doi already exists in Zotero collection tree"
            else:
                row["skip_reason"] = "normalized title plus year already exists in Zotero collection tree"
            row["matched_zotero_duplicate_type"] = match_type
            row["matched_zotero_duplicate_key"] = match_key
            row["matched_zotero_collection_key"] = zotero_record.get("collection_key", "")
            row["matched_zotero_collection_path"] = zotero_record.get("collection_path", "")
            row["matched_zotero_item_key"] = zotero_record.get("zotero_item_key", "")
            row["matched_zotero_title"] = zotero_record.get("zotero_title", "")
            row["matched_zotero_year"] = zotero_record.get("year", "")
            skipped.append(row)
        else:
            row["already_in_zotero"] = False
            row["skip_reason"] = ""
            kept.append(row)

    return kept, skipped
