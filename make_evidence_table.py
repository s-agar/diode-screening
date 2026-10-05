#!/usr/bin/env python3

import argparse
import csv
import html
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests


DEFAULT_INPUT_JSONL = "evidence_detection/evidence.jsonl"
DEFAULT_OUTPUT_HTML = "evidence_detection/evidence_table.html"
DEFAULT_OUTPUT_CSV = "evidence_detection/evidence_table.csv"
DEFAULT_ZOTERO_COLLECTION = "03_extracted"
ZOTERO_BASE_URL = "http://localhost:23119/api"


DECISION_ORDER = {
    "include_candidate": 0,
    "maybe": 1,
    "low_priority_maybe": 2,
    "exclude_candidate": 3,
    "error": 4,
}


DECISION_LABELS = {
    "include_candidate": "Include candidate",
    "maybe": "Maybe",
    "low_priority_maybe": "Low-priority maybe",
    "exclude_candidate": "Exclude candidate",
    "error": "Error",
}


DECISION_CLASSES = {
    "include_candidate": "decision-include",
    "maybe": "decision-maybe",
    "low_priority_maybe": "decision-low",
    "exclude_candidate": "decision-exclude",
    "error": "decision-error",
}


FIELD_LABELS = {
    "device_type": "Device type",
    "edge_termination": "Edge termination",
    "breakdown_voltage": "Breakdown voltage",
    "specific_on_resistance": "Specific on-resistance",
    "baliga_figure_of_merit": "Baliga figure of merit",
}


def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def escape(text: object) -> str:
    return html.escape("" if text is None else str(text))


def doi_to_url(doi: str) -> str:
    doi = clean_space(doi)
    if not doi:
        return ""

    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = doi.strip()

    if not doi:
        return ""

    return f"https://doi.org/{doi}"


def normalize_doi(doi: str) -> str:
    doi = clean_space(doi)
    if not doi:
        return ""

    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = doi.strip().strip(".;,)")
    return doi.lower()


def normalize_title(title: str) -> str:
    title = html.unescape(title or "").lower()
    title = re.sub(r"[^a-z0-9]+", " ", title)
    return re.sub(r"\s+", " ", title).strip()


def extract_year(value: object) -> str:
    text = str(value or "")
    match = re.search(r"\b(?:19|20)\d{2}\b", text)
    return match.group(0) if match else ""


def title_year_key(title: str, year: object) -> str:
    normalized_title = normalize_title(title)
    normalized_year = extract_year(year)
    if not normalized_title or not normalized_year:
        return ""
    return f"{normalized_title}::{normalized_year}"


def zotero_get_json(endpoint: str, params: Optional[dict] = None) -> object:
    url = f"{ZOTERO_BASE_URL}{endpoint}"
    response = requests.get(url, params=params, timeout=60)

    if response.status_code == 403:
        raise RuntimeError(
            "Zotero local API returned 403. Enable Zotero setting: "
            "Settings -> Advanced -> Allow other applications on this computer to communicate with Zotero."
        )

    response.raise_for_status()
    return response.json()


def zotero_get_json_paginated(endpoint: str, params: Optional[dict] = None, limit: int = 100) -> List[dict]:
    records = []
    start = 0

    while True:
        page_params = dict(params or {})
        page_params["limit"] = limit
        page_params["start"] = start

        page = zotero_get_json(endpoint, params=page_params)
        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected Zotero response for {endpoint}.")

        records.extend(page)

        if len(page) < limit:
            break

        start += limit

    return records


def collection_name(collection: dict) -> str:
    return (collection.get("data") or {}).get("name") or ""


def collection_key(collection: dict) -> str:
    return collection.get("key") or (collection.get("data") or {}).get("key") or ""


def parent_collection_key(collection: dict) -> str:
    return (collection.get("data") or {}).get("parentCollection") or ""


def build_collection_paths(collections: List[dict]) -> Dict[str, str]:
    by_key = {collection_key(c): c for c in collections if collection_key(c)}

    def path_for(key: str, seen: Optional[set] = None) -> str:
        if seen is None:
            seen = set()
        if key in seen:
            return collection_name(by_key[key])
        seen.add(key)

        collection = by_key.get(key)
        if not collection:
            return key

        name = collection_name(collection)
        parent = parent_collection_key(collection)

        if parent and parent in by_key:
            return f"{path_for(parent, seen)} / {name}"
        return name

    return {key: path_for(key) for key in by_key}


def find_collection(collection_name_or_key: str) -> Tuple[str, str]:
    collections = zotero_get_json_paginated("/users/0/collections")
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

    path_matches = [
        (key, path)
        for key, path in paths.items()
        if path == collection_name_or_key
    ]

    if len(path_matches) == 1:
        return path_matches[0]

    contains_matches = [
        (key, path)
        for key, path in paths.items()
        if collection_name_or_key.lower() in path.lower()
    ]

    if len(exact_name_matches) > 1 or contains_matches:
        print("Could not uniquely identify the Zotero collection. Possible matches:", file=sys.stderr)
        for key, path in (exact_name_matches or contains_matches):
            print(f"  {key}: {path}", file=sys.stderr)

    raise RuntimeError(f"Could not uniquely identify Zotero collection: {collection_name_or_key}")


def child_collection_keys(collections: List[dict], parent_key: str) -> List[str]:
    children = []
    stack = [parent_key]

    while stack:
        current = stack.pop()
        direct_children = [
            collection_key(c)
            for c in collections
            if parent_collection_key(c) == current and collection_key(c)
        ]
        children.extend(direct_children)
        stack.extend(direct_children)

    return children


def item_data(item: dict) -> dict:
    return item.get("data") or {}


def is_regular_item(item: dict) -> bool:
    return (item_data(item).get("itemType") or "") != "attachment"


def zotero_filter_identity_sets(collection_name_or_key: str, include_subcollections: bool = False) -> dict:
    collections = zotero_get_json_paginated("/users/0/collections")
    target_key, target_path = find_collection(collection_name_or_key)

    collection_keys = [target_key]
    if include_subcollections:
        collection_keys.extend(child_collection_keys(collections, target_key))

    zotero_keys: Set[str] = set()
    dois: Set[str] = set()
    title_years: Set[str] = set()

    for key in collection_keys:
        items = zotero_get_json_paginated(f"/users/0/collections/{key}/items")
        for item in items:
            if not is_regular_item(item):
                continue

            data = item_data(item)
            zotero_key = item.get("key") or data.get("key") or ""
            doi = normalize_doi(data.get("DOI") or data.get("doi") or "")
            ty_key = title_year_key(data.get("title") or "", data.get("date") or "")

            if zotero_key:
                zotero_keys.add(zotero_key)
            if doi:
                dois.add(doi)
            if ty_key:
                title_years.add(ty_key)

    return {
        "collection_key": target_key,
        "collection_path": target_path,
        "included_collection_keys": collection_keys,
        "zotero_keys": zotero_keys,
        "dois": dois,
        "title_years": title_years,
    }


def record_title_year_key(record: dict) -> str:
    metadata = get_metadata(record)
    title = metadata.get("title") or ""
    year = metadata.get("year") or metadata.get("date") or metadata.get("publication_year") or metadata.get("publication_date") or ""

    if not year:
        # Some older detector metadata only contains title/DOI/Zotero keys. Keep this
        # fallback conservative by refusing title-only matches.
        return ""

    return title_year_key(title, year)


def record_in_zotero_filter(record: dict, identity_sets: dict) -> bool:
    metadata = get_metadata(record)

    zotero_key = metadata.get("zotero_key") or ""
    doi = normalize_doi(metadata.get("doi") or "")
    ty_key = record_title_year_key(record)

    if zotero_key and zotero_key in identity_sets.get("zotero_keys", set()):
        return True
    if doi and doi in identity_sets.get("dois", set()):
        return True
    if ty_key and ty_key in identity_sets.get("title_years", set()):
        return True

    return False


def filter_records_to_zotero_collection(
    records: List[dict],
    collection_name_or_key: str,
    include_subcollections: bool = False,
) -> Tuple[List[dict], dict]:
    identity_sets = zotero_filter_identity_sets(
        collection_name_or_key=collection_name_or_key,
        include_subcollections=include_subcollections,
    )

    filtered = [record for record in records if record_in_zotero_filter(record, identity_sets)]

    summary = {
        "collection_key": identity_sets.get("collection_key", ""),
        "collection_path": identity_sets.get("collection_path", ""),
        "included_collection_count": len(identity_sets.get("included_collection_keys", [])),
        "zotero_item_key_count": len(identity_sets.get("zotero_keys", set())),
        "zotero_doi_count": len(identity_sets.get("dois", set())),
        "zotero_title_year_count": len(identity_sets.get("title_years", set())),
        "input_record_count": len(records),
        "filtered_record_count": len(filtered),
        "removed_record_count": len(records) - len(filtered),
    }

    return filtered, summary


def load_jsonl(path: Path) -> List[dict]:
    records = []

    with path.open("r", encoding="utf-8") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Could not parse JSONL line {line_number}: {exc}") from exc

    return records


def get_metadata(record: dict) -> dict:
    return record.get("metadata") or {}


def get_evidence(record: dict) -> List[dict]:
    evidence = record.get("evidence") or []
    if not isinstance(evidence, list):
        return []
    return evidence


def evidence_for_field(record: dict, field: str) -> List[dict]:
    return [
        ev for ev in get_evidence(record)
        if ev.get("field") == field
    ]


def sort_evidence(evidence: List[dict]) -> List[dict]:
    return sorted(
        evidence,
        key=lambda ev: (
            -safe_int(ev.get("score")),
            str(ev.get("source_type") or ""),
            str(ev.get("source_id") or ""),
            str(ev.get("snippet") or ""),
        ),
    )


def safe_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def format_score(value: object) -> str:
    try:
        number = float(value)
    except Exception:
        return ""

    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}"


def format_location(ev: dict) -> str:
    page = ev.get("page")
    source_id = ev.get("source_id") or ""
    source_type = ev.get("source_type") or ""

    parts = []

    if page not in [None, ""]:
        parts.append(f"p. {page}")

    if source_id:
        parts.append(str(source_id))

    if source_type:
        parts.append(str(source_type))

    return " · ".join(parts)


def format_value(ev: Optional[dict]) -> str:
    if not ev:
        return ""

    field = ev.get("field", "")

    if field == "edge_termination":
        value = ev.get("normalized_value") or ev.get("value") or ""
        raw = ev.get("raw_value") or ""
        if raw and raw != value:
            return f"{value} ({raw})"
        return str(value)

    if field in {"breakdown_voltage", "specific_on_resistance", "baliga_figure_of_merit"}:
        raw = ev.get("raw_value") or ""
        normalized = ev.get("normalized_value")
        normalized_unit = ev.get("normalized_unit") or ev.get("unit") or ""

        if normalized not in [None, ""]:
            try:
                normalized_text = f"{float(normalized):g} {normalized_unit}".strip()
            except Exception:
                normalized_text = f"{normalized} {normalized_unit}".strip()

            if raw:
                return f"{raw} → {normalized_text}"
            return normalized_text

        return str(raw)

    value = ev.get("normalized_value") or ev.get("value") or ev.get("raw_value") or ""
    return str(value)


def best_evidence(record: dict, field: str) -> Optional[dict]:
    field_evidence = sort_evidence(evidence_for_field(record, field))
    return field_evidence[0] if field_evidence else None


def top_evidence(record: dict, field: str, n: int = 3) -> List[dict]:
    return sort_evidence(evidence_for_field(record, field))[:n]


def count_field_evidence(record: dict, field: str) -> int:
    return len(evidence_for_field(record, field))


def field_status_class(record: dict, field: str) -> str:
    best = best_evidence(record, field)

    if not best:
        return "field-missing"

    score = safe_int(best.get("score"))

    if score >= 10:
        return "field-strong"
    if score >= 7:
        return "field-medium"
    return "field-weak"


def field_status_label(record: dict, field: str) -> str:
    best = best_evidence(record, field)

    if not best:
        return "Missing"

    score = safe_int(best.get("score"))

    if score >= 10:
        return "Strong"
    if score >= 7:
        return "Medium"
    return "Weak"


def risk_badges(ev: Optional[dict]) -> str:
    if not ev:
        return ""

    badges = []

    if ev.get("comparison_risk"):
        badges.append(("Comparison/literature risk", "badge-risk"))

    if ev.get("simulation_context") and not ev.get("measurement_context"):
        badges.append(("Simulation/design context", "badge-warn"))

    if ev.get("measurement_context"):
        badges.append(("Measurement context", "badge-good"))

    return " ".join(
        f'<span class="badge {css_class}">{escape(label)}</span>'
        for label, css_class in badges
    )


def render_field_cell(record: dict, field: str, max_snippets: int = 3) -> str:
    best = best_evidence(record, field)
    field_evs = top_evidence(record, field, n=max_snippets)
    status_class = field_status_class(record, field)
    status_label = field_status_label(record, field)
    count = count_field_evidence(record, field)

    if not best:
        return f"""
        <div class="field-card {status_class}">
          <div class="field-header">
            <span class="field-label">{escape(FIELD_LABELS.get(field, field))}</span>
            <span class="field-status">{escape(status_label)}</span>
          </div>
          <div class="field-value missing">No evidence detected</div>
          <div class="field-meta">0 matches</div>
        </div>
        """

    value = format_value(best)
    best_score = format_score(best.get("score"))
    location = format_location(best)

    snippets_html = []

    for ev in field_evs:
        snippet = clean_space(ev.get("snippet") or "")
        if len(snippet) > 900:
            snippet = snippet[:900].rstrip() + "..."

        snippets_html.append(
            f"""
            <details class="snippet-details">
              <summary>
                score {escape(format_score(ev.get("score")))}
                · {escape(format_location(ev))}
                {risk_badges(ev)}
              </summary>
              <div class="snippet-text">{escape(snippet)}</div>
            </details>
            """
        )

    snippets_joined = "\n".join(snippets_html)

    return f"""
    <div class="field-card {status_class}">
      <div class="field-header">
        <span class="field-label">{escape(FIELD_LABELS.get(field, field))}</span>
        <span class="field-status">{escape(status_label)}</span>
      </div>
      <div class="field-value">{escape(value)}</div>
      <div class="field-meta">
        best score {escape(best_score)}
        · {escape(str(count))} match{"es" if count != 1 else ""}
        {f" · {escape(location)}" if location else ""}
      </div>
      {risk_badges(best)}
      <div class="snippets">
        {snippets_joined}
      </div>
    </div>
    """


def render_paper_card(record: dict, index: int) -> str:
    metadata = get_metadata(record)

    title = metadata.get("title") or "(Untitled)"
    doi = metadata.get("doi") or ""
    doi_url = doi_to_url(doi)
    zotero_key = metadata.get("zotero_key") or ""
    attachment_key = metadata.get("attachment_key") or ""
    pdf_path = metadata.get("source_pdf_path") or ""
    output_dir = metadata.get("output_dir") or ""

    decision = record.get("decision") or "unknown"
    decision_label = DECISION_LABELS.get(decision, decision)
    decision_class = DECISION_CLASSES.get(decision, "decision-unknown")
    decision_reason = record.get("decision_reason") or ""

    best_device = best_evidence(record, "device_type")
    device_type = format_value(best_device) if best_device else ""

    edge_cell = render_field_cell(record, "edge_termination")
    bv_cell = render_field_cell(record, "breakdown_voltage")
    ron_cell = render_field_cell(record, "specific_on_resistance")
    bfom_cell = render_field_cell(record, "baliga_figure_of_merit")

    pdf_link = ""
    if pdf_path:
        pdf_uri = Path(pdf_path).expanduser().resolve().as_uri() if Path(pdf_path).exists() else ""
        if pdf_uri:
            pdf_link = f'<a href="{escape(pdf_uri)}">Open PDF</a>'

    output_link = ""
    if output_dir:
        output_path = Path(output_dir).expanduser()
        if output_path.exists():
            output_link = f'<a href="{escape(output_path.resolve().as_uri())}">Extraction folder</a>'

    links = " · ".join([link for link in [pdf_link, output_link] if link])

    doi_html = ""
    if doi:
        doi_html = (
            f'<span class="muted">DOI:</span> {escape(doi)} '
            f'<button type="button" class="copy-button copy-doi-link" '
            f'data-copy-text="{escape(doi_url)}" title="Copy DOI link">Copy DOI link</button>'
        )
    zotero_html = f'<span class="muted">Zotero:</span> {escape(zotero_key)}' if zotero_key else ""
    attachment_html = f'<span class="muted">Attachment:</span> {escape(attachment_key)}' if attachment_key else ""
    device_html = f'<span class="muted">Device guess:</span> {escape(device_type)}' if device_type else ""

    meta_items = " · ".join(
        item for item in [doi_html, zotero_html, attachment_html, device_html, links]
        if item
    )

    return f"""
    <article class="paper-card" data-decision="{escape(decision)}">
      <div class="paper-topline">
        <div class="paper-index">#{index}</div>
        <div class="decision-pill {escape(decision_class)}">{escape(decision_label)}</div>
      </div>

      <h2>
        <span class="paper-title-text">{escape(title)}</span>
        <button type="button" class="copy-button copy-title" data-copy-text="{escape(title)}" title="Copy title">Copy title</button>
      </h2>

      <div class="paper-meta">
        {meta_items}
      </div>

      <div class="decision-reason">
        {escape(decision_reason)}
      </div>

      <div class="field-grid">
        {edge_cell}
        {bv_cell}
        {ron_cell}
        {bfom_cell}
      </div>
    </article>
    """


def sort_records(records: List[dict]) -> List[dict]:
    def sort_key(record: dict):
        metadata = get_metadata(record)
        decision = record.get("decision") or "unknown"

        edge_best = best_evidence(record, "edge_termination")
        bv_best = best_evidence(record, "breakdown_voltage")
        ron_best = best_evidence(record, "specific_on_resistance")
        bfom_best = best_evidence(record, "baliga_figure_of_merit")

        edge_score = safe_int(edge_best.get("score")) if edge_best else 0
        bv_score = safe_int(bv_best.get("score")) if bv_best else 0
        ron_score = safe_int(ron_best.get("score")) if ron_best else 0
        bfom_score = safe_int(bfom_best.get("score")) if bfom_best else 0

        return (
            DECISION_ORDER.get(decision, 99),
            -min(edge_score, bv_score, ron_score, bfom_score),
            -(edge_score + bv_score + ron_score + bfom_score),
            (metadata.get("title") or "").lower(),
        )

    return sorted(records, key=sort_key)


def summarize_records(records: List[dict]) -> Dict[str, int]:
    counts = {}

    for record in records:
        decision = record.get("decision") or "unknown"
        counts[decision] = counts.get(decision, 0) + 1

    return counts


def render_summary(counts: Dict[str, int], total: int) -> str:
    cards = []

    for decision in [
        "include_candidate",
        "maybe",
        "low_priority_maybe",
        "exclude_candidate",
        "error",
    ]:
        count = counts.get(decision, 0)
        label = DECISION_LABELS.get(decision, decision)
        css_class = DECISION_CLASSES.get(decision, "decision-unknown")

        cards.append(
            f"""
            <div class="summary-card {escape(css_class)}">
              <div class="summary-count">{count}</div>
              <div class="summary-label">{escape(label)}</div>
            </div>
            """
        )

    return f"""
    <section class="summary">
      <div class="summary-card total">
        <div class="summary-count">{total}</div>
        <div class="summary-label">Total papers</div>
      </div>
      {''.join(cards)}
    </section>
    """


def render_html(records: List[dict]) -> str:
    sorted_records = sort_records(records)
    counts = summarize_records(sorted_records)

    paper_cards = "\n".join(
        render_paper_card(record, index)
        for index, record in enumerate(sorted_records, start=1)
    )

    summary_html = render_summary(counts, len(sorted_records))

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>SiC diode evidence table</title>
  <style>
    :root {{
      --bg: #f7f8fb;
      --card: #ffffff;
      --text: #1f2937;
      --muted: #6b7280;
      --border: #d9dde6;
      --soft-border: #edf0f5;
      --include: #d8f5df;
      --include-text: #166534;
      --maybe: #fff3c4;
      --maybe-text: #854d0e;
      --low: #ffe4cc;
      --low-text: #9a3412;
      --exclude: #eeeeee;
      --exclude-text: #4b5563;
      --error: #ffd6d6;
      --error-text: #991b1b;
      --strong: #ddf7e6;
      --medium: #fff4c7;
      --weak: #ffe3ce;
      --missing: #eeeeee;
      --link: #1d4ed8;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      padding: 32px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.45;
    }}

    header {{
      max-width: 1500px;
      margin: 0 auto 24px auto;
    }}

    h1 {{
      margin: 0 0 8px 0;
      font-size: 30px;
      letter-spacing: -0.02em;
    }}

    .subtitle {{
      color: var(--muted);
      font-size: 15px;
    }}

    .summary {{
      max-width: 1500px;
      margin: 24px auto;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
    }}

    .summary-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
    }}

    .summary-card.total {{
      background: #eef2ff;
    }}

    .summary-count {{
      font-size: 28px;
      font-weight: 750;
    }}

    .summary-label {{
      color: var(--muted);
      font-size: 13px;
      margin-top: 2px;
    }}

    .paper-card {{
      max-width: 1500px;
      margin: 18px auto;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 22px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.04);
    }}

    .paper-topline {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 8px;
    }}

    .paper-index {{
      font-size: 13px;
      color: var(--muted);
      font-weight: 600;
    }}

    h2 {{
      margin: 0;
      font-size: 20px;
      line-height: 1.25;
      letter-spacing: -0.01em;
    }}

    .paper-title-text {{
      vertical-align: middle;
    }}

    .copy-button {{
      appearance: none;
      border: 1px solid var(--border);
      border-radius: 999px;
      background: #ffffff;
      color: #374151;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font: inherit;
      font-size: 12px;
      font-weight: 750;
      line-height: 1;
      margin-left: 8px;
      padding: 5px 9px;
      vertical-align: middle;
      white-space: nowrap;
    }}

    .copy-button:hover {{
      border-color: #9ca3af;
      background: #f9fafb;
    }}

    .copy-button:active {{
      transform: translateY(1px);
    }}

    .copy-button.copied {{
      background: #dcfce7;
      border-color: #86efac;
      color: #166534;
    }}

    .copy-doi-link {{
      margin-left: 6px;
      padding: 4px 8px;
    }}

    .paper-meta {{
      margin-top: 10px;
      color: var(--muted);
      font-size: 13px;
    }}

    .paper-meta a {{
      color: var(--link);
      text-decoration: none;
      font-weight: 600;
    }}

    .paper-meta a:hover {{
      text-decoration: underline;
    }}

    .muted {{
      color: var(--muted);
      font-weight: 600;
    }}

    .decision-reason {{
      margin-top: 12px;
      padding: 10px 12px;
      background: #f8fafc;
      border: 1px solid var(--soft-border);
      border-radius: 10px;
      font-size: 14px;
      color: #374151;
    }}

    .decision-pill {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 11px;
      font-size: 12px;
      font-weight: 750;
      white-space: nowrap;
    }}

    .decision-include {{
      background: var(--include);
      color: var(--include-text);
    }}

    .decision-maybe {{
      background: var(--maybe);
      color: var(--maybe-text);
    }}

    .decision-low {{
      background: var(--low);
      color: var(--low-text);
    }}

    .decision-exclude {{
      background: var(--exclude);
      color: var(--exclude-text);
    }}

    .decision-error {{
      background: var(--error);
      color: var(--error-text);
    }}

    .field-grid {{
      margin-top: 16px;
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
    }}

    @media (max-width: 1100px) {{
      .field-grid {{
        grid-template-columns: 1fr;
      }}

      body {{
        padding: 18px;
      }}
    }}

    .field-card {{
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 14px;
      min-width: 0;
    }}

    .field-strong {{
      background: var(--strong);
    }}

    .field-medium {{
      background: var(--medium);
    }}

    .field-weak {{
      background: var(--weak);
    }}

    .field-missing {{
      background: var(--missing);
    }}

    .field-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
    }}

    .field-label {{
      font-weight: 800;
      font-size: 14px;
    }}

    .field-status {{
      font-size: 12px;
      font-weight: 800;
      color: #374151;
    }}

    .field-value {{
      font-size: 15px;
      font-weight: 750;
      margin-bottom: 5px;
      word-break: break-word;
    }}

    .field-value.missing {{
      color: var(--muted);
      font-weight: 650;
    }}

    .field-meta {{
      color: #4b5563;
      font-size: 12px;
      margin-bottom: 8px;
    }}

    .badge {{
      display: inline-flex;
      margin: 2px 4px 2px 0;
      padding: 3px 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 750;
      border: 1px solid rgba(0, 0, 0, 0.08);
    }}

    .badge-risk {{
      background: #fee2e2;
      color: #991b1b;
    }}

    .badge-warn {{
      background: #ffedd5;
      color: #9a3412;
    }}

    .badge-good {{
      background: #dcfce7;
      color: #166534;
    }}

    .snippets {{
      margin-top: 8px;
    }}

    .snippet-details {{
      background: rgba(255, 255, 255, 0.62);
      border: 1px solid rgba(0, 0, 0, 0.07);
      border-radius: 10px;
      padding: 7px 9px;
      margin-top: 7px;
      font-size: 12px;
    }}

    .snippet-details summary {{
      cursor: pointer;
      font-weight: 700;
      color: #374151;
    }}

    .snippet-text {{
      margin-top: 8px;
      color: #111827;
      font-size: 12px;
      white-space: normal;
    }}

    footer {{
      max-width: 1500px;
      margin: 28px auto 0 auto;
      color: var(--muted);
      font-size: 12px;
    }}
  </style>
</head>
<body>
  <header>
    <h1>SiC diode evidence table</h1>
    <div class="subtitle">
      Evidence summary for edge termination, breakdown voltage, and specific on-resistance.
      Open each evidence item to inspect the source snippet.
    </div>
  </header>

  {summary_html}

  <main>
    {paper_cards}
  </main>

  <footer>
    Generated from evidence JSONL. Decisions are automated triage labels and should be manually reviewed.
  </footer>

  <script>
    async function copyTextToClipboard(text) {{
      if (navigator.clipboard && window.isSecureContext) {{
        await navigator.clipboard.writeText(text);
        return;
      }}

      const textArea = document.createElement("textarea");
      textArea.value = text;
      textArea.style.position = "fixed";
      textArea.style.left = "-9999px";
      textArea.style.top = "-9999px";
      document.body.appendChild(textArea);
      textArea.focus();
      textArea.select();

      try {{
        document.execCommand("copy");
      }} finally {{
        document.body.removeChild(textArea);
      }}
    }}

    document.addEventListener("click", async function(event) {{
      const button = event.target.closest(".copy-button");
      if (!button) {{
        return;
      }}

      const text = button.getAttribute("data-copy-text") || "";
      if (!text) {{
        return;
      }}

      const originalText = button.textContent;

      try {{
        await copyTextToClipboard(text);
        button.textContent = "Copied";
        button.classList.add("copied");
        window.setTimeout(function() {{
          button.textContent = originalText;
          button.classList.remove("copied");
        }}, 1200);
      }} catch (error) {{
        button.textContent = "Copy failed";
        window.setTimeout(function() {{
          button.textContent = originalText;
        }}, 1600);
      }}
    }});
  </script>
</body>
</html>
"""


def row_for_csv(record: dict) -> dict:
    metadata = get_metadata(record)

    best_device = best_evidence(record, "device_type")
    best_edge = best_evidence(record, "edge_termination")
    best_bv = best_evidence(record, "breakdown_voltage")
    best_ron = best_evidence(record, "specific_on_resistance")
    best_bfom = best_evidence(record, "baliga_figure_of_merit")

    def snippet(ev: Optional[dict]) -> str:
        return clean_space(ev.get("snippet", "")) if ev else ""

    def score(ev: Optional[dict]) -> str:
        return format_score(ev.get("score")) if ev else ""

    def loc(ev: Optional[dict]) -> str:
        return format_location(ev) if ev else ""

    return {
        "decision": record.get("decision", ""),
        "decision_reason": record.get("decision_reason", ""),
        "title": metadata.get("title", ""),
        "doi": metadata.get("doi", ""),
        "zotero_key": metadata.get("zotero_key", ""),
        "attachment_key": metadata.get("attachment_key", ""),
        "source_pdf_path": metadata.get("source_pdf_path", ""),
        "output_dir": metadata.get("output_dir", ""),

        "device_type": format_value(best_device),
        "device_type_score": score(best_device),
        "device_type_location": loc(best_device),
        "device_type_snippet": snippet(best_device),

        "edge_termination": format_value(best_edge),
        "edge_score": score(best_edge),
        "edge_location": loc(best_edge),
        "edge_snippet": snippet(best_edge),

        "breakdown_voltage": format_value(best_bv),
        "breakdown_score": score(best_bv),
        "breakdown_location": loc(best_bv),
        "breakdown_snippet": snippet(best_bv),

        "specific_on_resistance": format_value(best_ron),
        "specific_on_resistance_score": score(best_ron),
        "specific_on_resistance_location": loc(best_ron),
        "specific_on_resistance_snippet": snippet(best_ron),

        "baliga_figure_of_merit": format_value(best_bfom),
        "baliga_figure_of_merit_score": score(best_bfom),
        "baliga_figure_of_merit_location": loc(best_bfom),
        "baliga_figure_of_merit_snippet": snippet(best_bfom),

        "n_edge_evidence": count_field_evidence(record, "edge_termination"),
        "n_breakdown_evidence": count_field_evidence(record, "breakdown_voltage"),
        "n_specific_on_resistance_evidence": count_field_evidence(record, "specific_on_resistance"),
        "n_baliga_figure_of_merit_evidence": count_field_evidence(record, "baliga_figure_of_merit"),
    }


def write_csv(records: List[dict], path: Path) -> None:
    sorted_records = sort_records(records)

    fieldnames = [
        "decision",
        "decision_reason",
        "title",
        "doi",
        "zotero_key",
        "attachment_key",
        "source_pdf_path",
        "output_dir",

        "device_type",
        "device_type_score",
        "device_type_location",
        "device_type_snippet",

        "edge_termination",
        "edge_score",
        "edge_location",
        "edge_snippet",

        "breakdown_voltage",
        "breakdown_score",
        "breakdown_location",
        "breakdown_snippet",

        "specific_on_resistance",
        "specific_on_resistance_score",
        "specific_on_resistance_location",
        "specific_on_resistance_snippet",

        "baliga_figure_of_merit",
        "baliga_figure_of_merit_score",
        "baliga_figure_of_merit_location",
        "baliga_figure_of_merit_snippet",

        "n_edge_evidence",
        "n_breakdown_evidence",
        "n_specific_on_resistance_evidence",
        "n_baliga_figure_of_merit_evidence",
    ]

    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for record in sorted_records:
            writer.writerow(row_for_csv(record))


def write_html(records: List[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_html(records)
    path.write_text(html_text, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a readable HTML/CSV evidence table from detect_sic_diode_evidence.py output."
    )

    parser.add_argument(
        "--input-jsonl",
        default=DEFAULT_INPUT_JSONL,
        help=f"Input evidence JSONL. Default: {DEFAULT_INPUT_JSONL}",
    )

    parser.add_argument(
        "--output-html",
        default=DEFAULT_OUTPUT_HTML,
        help=f"Output HTML table. Default: {DEFAULT_OUTPUT_HTML}",
    )

    parser.add_argument(
        "--output-csv",
        default=DEFAULT_OUTPUT_CSV,
        help=f"Output CSV table. Default: {DEFAULT_OUTPUT_CSV}",
    )

    parser.add_argument(
        "--zotero-filter-collection",
        default=DEFAULT_ZOTERO_COLLECTION,
        help=(
            "Only include records whose Zotero item is currently in this collection. "
            f"Default: {DEFAULT_ZOTERO_COLLECTION}"
        ),
    )

    parser.add_argument(
        "--include-subcollections",
        action="store_true",
        help="When filtering by Zotero collection, also include records in subcollections of the filter collection.",
    )

    parser.add_argument(
        "--no-zotero-filter",
        action="store_true",
        help="Disable Zotero collection filtering and render all records from the input JSONL.",
    )

    args = parser.parse_args()

    input_jsonl = Path(args.input_jsonl)
    output_html = Path(args.output_html)
    output_csv = Path(args.output_csv)

    if not input_jsonl.exists():
        raise SystemExit(f"Input JSONL not found: {input_jsonl}")

    all_records = load_jsonl(input_jsonl)

    if args.no_zotero_filter:
        records = all_records
        filter_summary = None
    else:
        records, filter_summary = filter_records_to_zotero_collection(
            records=all_records,
            collection_name_or_key=args.zotero_filter_collection,
            include_subcollections=args.include_subcollections,
        )

    write_html(records, output_html)
    write_csv(records, output_csv)

    print("Done.")
    print(f"Input records: {len(all_records)}")
    print(f"Output records: {len(records)}")

    if filter_summary:
        print(f"Zotero filter collection: {filter_summary.get('collection_path')} [{filter_summary.get('collection_key')}]")
        print(f"Zotero collection item keys: {filter_summary.get('zotero_item_key_count')}")
        print(f"Records removed by Zotero collection filter: {filter_summary.get('removed_record_count')}")

    print(f"HTML evidence table: {output_html}")
    print(f"CSV evidence table: {output_csv}")


if __name__ == "__main__":
    main()