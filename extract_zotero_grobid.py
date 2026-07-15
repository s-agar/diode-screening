#!/usr/bin/env python3

# To start grobid: docker run --rm --init --ulimit core=0 -p 8070:8070 grobid/grobid:0.9.0-crf

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


ZOTERO_BASE_URL = "http://localhost:23119/api"
GROBID_BASE_URL = "http://localhost:8070"

DEFAULT_COLLECTION_NAME = "01_pdf_available"
DEFAULT_OUTPUT_DIR = "extracted_zotero_grobid"

TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


MANIFEST_FIELDNAMES = [
    "run_id",
    "status",
    "skip_reason",
    "matched_existing_by",
    "existing_output_dir",
    "paper_id",
    "zotero_key",
    "attachment_key",
    "title",
    "doi",
    "date",
    "pdf_path",
    "pdf_sha1",
    "output_dir",
    "grobid_status",
    "pymupdf_status",
    "pdfplumber_status",
    "error",
]


def slugify(value: str, max_len: int = 80) -> str:
    value = value or "untitled"
    value = re.sub(r"[^\w\s.-]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value.strip())
    value = value.strip("._")
    if not value:
        value = "untitled"
    return value[:max_len]


def sha1_file(path: Path, block_size: int = 1024 * 1024) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def timestamp_for_filename() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_read_json(path: Path) -> object:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def normalize_doi(doi: str) -> str:
    doi = doi or ""
    doi = doi.strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.IGNORECASE)
    doi = doi.strip().strip(". ,;:")
    return doi.lower()


def normalize_title(title: str) -> str:
    title = title or ""
    title = title.lower()
    title = re.sub(r"<[^>]+>", " ", title)
    title = re.sub(r"[^a-z0-9]+", " ", title)
    title = re.sub(r"\s+", " ", title).strip()
    return title


def extract_year_from_date(date_text: str) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", date_text or "")
    return match.group(0) if match else ""


def title_year_key(title: str, date_text: str) -> str:
    title_norm = normalize_title(title)
    year = extract_year_from_date(date_text)
    if not title_norm or not year:
        return ""
    return f"{title_norm}|{year}"


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


def zotero_get_text(endpoint: str, params: Optional[dict] = None) -> str:
    url = f"{ZOTERO_BASE_URL}{endpoint}"
    response = requests.get(url, params=params, timeout=60)

    if response.status_code == 403:
        raise RuntimeError(
            "Zotero local API returned 403. Enable Zotero setting: "
            "Settings -> Advanced -> Allow other applications on this computer to communicate with Zotero."
        )

    response.raise_for_status()
    return response.text.strip()


def zotero_get_all_json(endpoint: str, params: Optional[dict] = None, page_size: int = 100) -> List[dict]:
    all_items = []
    start = 0

    while True:
        page_params = dict(params or {})
        page_params["limit"] = page_size
        page_params["start"] = start

        page = zotero_get_json(endpoint, params=page_params)
        if not isinstance(page, list):
            raise RuntimeError(f"Unexpected Zotero response for {endpoint}.")

        all_items.extend(page)

        if len(page) < page_size:
            break

        start += page_size

    return all_items


def check_zotero() -> None:
    response = requests.get(f"{ZOTERO_BASE_URL}/users/0/items", params={"limit": 1}, timeout=20)
    if response.status_code == 403:
        raise RuntimeError(
            "Zotero local API is not enabled. In Zotero, enable: "
            "Settings -> Advanced -> Allow other applications on this computer to communicate with Zotero."
        )
    response.raise_for_status()


def check_grobid(grobid_url: str) -> None:
    url = f"{grobid_url.rstrip('/')}/api/isalive"
    response = requests.get(url, timeout=20)
    response.raise_for_status()
    if "true" not in response.text.lower():
        raise RuntimeError(f"GROBID did not report alive at {url}. Response: {response.text}")


def get_all_collections() -> List[dict]:
    return zotero_get_all_json("/users/0/collections")


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

        c = by_key.get(key)
        if not c:
            return key

        name = collection_name(c)
        parent = parent_collection_key(c)

        if parent and parent in by_key:
            return f"{path_for(parent, seen)} / {name}"
        return name

    return {key: path_for(key) for key in by_key}


def find_collection(collection_name_or_key: str) -> Tuple[str, str]:
    collections = get_all_collections()
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
        print("Multiple collections have that exact name. Matching paths:", file=sys.stderr)
        for key, path in exact_name_matches:
            print(f"  {key}: {path}", file=sys.stderr)
        raise RuntimeError("Use the collection key instead of the collection name.")

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

    if len(contains_matches) == 1:
        return contains_matches[0]

    if contains_matches:
        print("No exact match. Possible collection matches:", file=sys.stderr)
        for key, path in contains_matches:
            print(f"  {key}: {path}", file=sys.stderr)
    else:
        print("No matching collection found. Available collections:", file=sys.stderr)
        for key, path in sorted(paths.items(), key=lambda kv: kv[1].lower()):
            print(f"  {key}: {path}", file=sys.stderr)

    raise RuntimeError(f"Could not uniquely identify collection: {collection_name_or_key}")


def get_descendant_collection_keys(root_key: str) -> List[str]:
    collections = get_all_collections()
    children_by_parent: Dict[str, List[str]] = {}

    for collection in collections:
        key = collection_key(collection)
        parent = parent_collection_key(collection)
        if key and parent:
            children_by_parent.setdefault(parent, []).append(key)

    keys = [root_key]
    stack = [root_key]

    while stack:
        parent = stack.pop()
        for child in children_by_parent.get(parent, []):
            if child not in keys:
                keys.append(child)
                stack.append(child)

    return keys


def get_collection_items(collection_key_value: str) -> List[dict]:
    endpoint = f"/users/0/collections/{collection_key_value}/items"
    return zotero_get_all_json(endpoint)


def get_item_children(item_key_value: str) -> List[dict]:
    endpoint = f"/users/0/items/{item_key_value}/children"
    return zotero_get_all_json(endpoint)


def item_data(item: dict) -> dict:
    return item.get("data") or {}


def is_regular_item(item: dict) -> bool:
    data = item_data(item)
    item_type = data.get("itemType") or ""
    return item_type != "attachment"


def is_pdf_attachment(item: dict) -> bool:
    data = item_data(item)
    if data.get("itemType") != "attachment":
        return False

    content_type = (data.get("contentType") or "").lower()
    title = (data.get("title") or "").lower()
    filename = (data.get("filename") or "").lower()

    return (
        content_type == "application/pdf"
        or title.endswith(".pdf")
        or filename.endswith(".pdf")
    )


def get_attachment_file_url(attachment_key: str) -> Optional[str]:
    endpoint = f"/users/0/items/{attachment_key}/file/view/url"

    try:
        text = zotero_get_text(endpoint)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in {404, 409}:
            return None
        raise

    return text or None


def file_url_to_path(file_url: str) -> Optional[Path]:
    if not file_url:
        return None

    parsed = urllib.parse.urlparse(file_url)

    if parsed.scheme != "file":
        return None

    path = urllib.parse.unquote(parsed.path)

    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]

    return Path(path)


def get_pdf_attachments_for_item(item_key_value: str) -> List[dict]:
    children = get_item_children(item_key_value)
    pdfs = []

    for child in children:
        if not is_pdf_attachment(child):
            continue

        attachment_key = child.get("key") or item_data(child).get("key")
        if not attachment_key:
            continue

        file_url = get_attachment_file_url(attachment_key)
        pdf_path = file_url_to_path(file_url) if file_url else None

        attachment_record = {
            "attachment_key": attachment_key,
            "attachment_title": item_data(child).get("title") or "",
            "attachment_filename": item_data(child).get("filename") or "",
            "file_url": file_url or "",
            "pdf_path": str(pdf_path) if pdf_path and pdf_path.exists() else "",
        }
        pdfs.append(attachment_record)

    return pdfs


def grobid_process_fulltext(
    pdf_path: Path,
    grobid_url: str,
    timeout: int = 180,
    max_retries: int = 3,
) -> Tuple[Optional[str], Optional[str]]:
    url = f"{grobid_url.rstrip('/')}/api/processFulltextDocument"

    form_data = {
        "consolidateHeader": "0",
        "consolidateCitations": "0",
        "includeRawCitations": "1",
        "includeRawAffiliations": "1",
        "teiCoordinates": ["figure", "ref", "biblStruct", "formula", "s"],
    }

    headers = {
        "Accept": "application/xml",
    }

    for attempt in range(1, max_retries + 1):
        with pdf_path.open("rb") as f:
            files = {
                "input": (pdf_path.name, f, "application/pdf"),
            }

            try:
                response = requests.post(
                    url,
                    headers=headers,
                    files=files,
                    data=form_data,
                    timeout=timeout,
                )
            except requests.RequestException as exc:
                if attempt == max_retries:
                    return None, f"request_exception: {exc}"
                time.sleep(5 * attempt)
                continue

        if response.status_code == 200 and response.text.strip():
            return response.text, None

        if response.status_code == 204:
            return None, "grobid_204_no_structured_content"

        if response.status_code == 503 and attempt < max_retries:
            time.sleep(8 * attempt)
            continue

        if response.status_code >= 400:
            return None, f"grobid_http_{response.status_code}: {response.text[:500]}"

    return None, "grobid_unknown_failure"


def xml_text(element: Optional[ET.Element]) -> str:
    if element is None:
        return ""
    return re.sub(r"\s+", " ", "".join(element.itertext())).strip()


def extract_grobid_metadata_and_sections(tei_xml: str) -> dict:
    result = {
        "title": "",
        "abstract": "",
        "sections": [],
        "figures": [],
        "tables": [],
    }

    try:
        root = ET.fromstring(tei_xml)
    except ET.ParseError as exc:
        result["parse_error"] = str(exc)
        return result

    title_el = root.find(".//tei:titleStmt/tei:title", TEI_NS)
    result["title"] = xml_text(title_el)

    abstract_el = root.find(".//tei:profileDesc/tei:abstract", TEI_NS)
    result["abstract"] = xml_text(abstract_el)

    body = root.find(".//tei:text/tei:body", TEI_NS)
    if body is not None:
        for div in body.findall(".//tei:div", TEI_NS):
            head = xml_text(div.find("tei:head", TEI_NS))
            paragraphs = [
                xml_text(p)
                for p in div.findall("tei:p", TEI_NS)
                if xml_text(p)
            ]

            if head or paragraphs:
                result["sections"].append({
                    "heading": head,
                    "text": "\n\n".join(paragraphs),
                })

    for fig in root.findall(".//tei:figure", TEI_NS):
        fig_type = fig.attrib.get("type", "")
        head = xml_text(fig.find("tei:head", TEI_NS))
        label = xml_text(fig.find("tei:label", TEI_NS))
        fig_desc = xml_text(fig.find("tei:figDesc", TEI_NS))
        full_text = xml_text(fig)

        record = {
            "type": fig_type or "figure",
            "label": label,
            "head": head,
            "caption": fig_desc,
            "text": full_text,
            "attributes": dict(fig.attrib),
        }

        if fig_type == "table":
            result["tables"].append(record)
        else:
            result["figures"].append(record)

    return result


def write_plain_text_from_grobid(parsed: dict, out_path: Path) -> None:
    lines = []

    if parsed.get("title"):
        lines.append(parsed["title"])
        lines.append("=" * len(parsed["title"]))
        lines.append("")

    if parsed.get("abstract"):
        lines.append("ABSTRACT")
        lines.append(parsed["abstract"])
        lines.append("")

    for section in parsed.get("sections", []):
        heading = section.get("heading") or "Untitled section"
        text = section.get("text") or ""

        lines.append(heading)
        lines.append("-" * min(len(heading), 80))
        lines.append(text)
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def extract_pages_and_images_with_pymupdf(pdf_path: Path, outdir: Path) -> dict:
    result = {
        "pages": [],
        "images": [],
        "error": "",
    }

    if fitz is None:
        result["error"] = "PyMuPDF is not installed. Run: pip install pymupdf"
        return result

    image_dir = outdir / "images"
    ensure_dir(image_dir)

    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        result["error"] = f"pymupdf_open_error: {exc}"
        return result

    try:
        for page_index in range(len(doc)):
            page = doc[page_index]

            try:
                page_text = page.get_text("text")
            except Exception:
                page_text = ""

            result["pages"].append({
                "page_number": page_index + 1,
                "text": page_text,
            })

            try:
                images = page.get_images(full=True)
            except Exception:
                images = []

            for image_index, image_info in enumerate(images, start=1):
                xref = image_info[0]
                try:
                    extracted = doc.extract_image(xref)
                    image_bytes = extracted.get("image")
                    ext = extracted.get("ext") or "bin"

                    if not image_bytes:
                        continue

                    filename = f"page_{page_index + 1:04d}_image_{image_index:03d}.{ext}"
                    image_path = image_dir / filename
                    image_path.write_bytes(image_bytes)

                    result["images"].append({
                        "page_number": page_index + 1,
                        "image_index": image_index,
                        "xref": xref,
                        "filename": filename,
                        "path": str(image_path),
                        "ext": ext,
                    })
                except Exception as exc:
                    result["images"].append({
                        "page_number": page_index + 1,
                        "image_index": image_index,
                        "xref": xref,
                        "error": str(exc),
                    })
    finally:
        doc.close()

    return result


def extract_tables_with_pdfplumber(pdf_path: Path, outdir: Path) -> dict:
    result = {
        "tables": [],
        "error": "",
    }

    if pdfplumber is None:
        result["error"] = "pdfplumber is not installed. Run: pip install pdfplumber"
        return result

    table_dir = outdir / "pdfplumber_tables"
    ensure_dir(table_dir)

    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                try:
                    tables = page.extract_tables()
                except Exception as exc:
                    result["tables"].append({
                        "page_number": page_index,
                        "error": str(exc),
                    })
                    continue

                for table_index, table in enumerate(tables or [], start=1):
                    if not table:
                        continue

                    filename = f"page_{page_index:04d}_table_{table_index:03d}.csv"
                    table_path = table_dir / filename

                    with table_path.open("w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        for row in table:
                            writer.writerow(row)

                    result["tables"].append({
                        "page_number": page_index,
                        "table_index": table_index,
                        "filename": filename,
                        "path": str(table_path),
                        "n_rows": len(table),
                    })
    except Exception as exc:
        result["error"] = f"pdfplumber_error: {exc}"

    return result


def extract_metadata_from_item(item: dict) -> dict:
    data = item_data(item)

    creators = []
    for creator in data.get("creators") or []:
        first = creator.get("firstName") or ""
        last = creator.get("lastName") or ""
        name = creator.get("name") or " ".join([first, last]).strip()
        if name:
            creators.append(name)

    return {
        "zotero_key": item.get("key") or data.get("key") or "",
        "item_type": data.get("itemType") or "",
        "title": data.get("title") or "",
        "creators": creators,
        "date": data.get("date") or "",
        "publication_title": data.get("publicationTitle") or data.get("proceedingsTitle") or "",
        "doi": data.get("DOI") or "",
        "url": data.get("url") or "",
    }


def generated_paper_id(item_metadata: dict, attachment: dict, pdf_path: Path) -> str:
    safe_title = slugify(item_metadata.get("title") or pdf_path.stem)
    paper_id = f"{safe_title}_{item_metadata.get('zotero_key', '')}_{attachment.get('attachment_key', '')}"
    return slugify(paper_id, max_len=140)


def component_paths(paper_dir: Path) -> dict:
    return {
        "tei": paper_dir / "fulltext.tei.xml",
        "parsed": paper_dir / "grobid_parsed.json",
        "text": paper_dir / "fulltext.txt",
        "pages": paper_dir / "pages.json",
        "images": paper_dir / "images.json",
        "tables": paper_dir / "tables.json",
        "metadata": paper_dir / "metadata.json",
    }


def grobid_component_complete(paper_dir: Path) -> bool:
    paths = component_paths(paper_dir)
    return paths["tei"].exists() and paths["parsed"].exists() and paths["text"].exists()


def pymupdf_component_complete(paper_dir: Path) -> bool:
    paths = component_paths(paper_dir)
    return paths["pages"].exists() and paths["images"].exists()


def pdfplumber_component_complete(paper_dir: Path) -> bool:
    paths = component_paths(paper_dir)
    return paths["tables"].exists()


def extraction_complete(paper_dir: Path) -> bool:
    paths = component_paths(paper_dir)
    return (
        paths["metadata"].exists()
        and grobid_component_complete(paper_dir)
        and pymupdf_component_complete(paper_dir)
        and pdfplumber_component_complete(paper_dir)
    )


def record_from_existing_dir(paper_dir: Path) -> Optional[dict]:
    metadata_path = paper_dir / "metadata.json"
    metadata = safe_read_json(metadata_path)
    if not isinstance(metadata, dict):
        return None

    item = metadata.get("item") or {}
    attachment = metadata.get("attachment") or {}

    title = item.get("title") or ""
    date_text = item.get("date") or ""
    doi = normalize_doi(item.get("doi") or item.get("DOI") or "")
    attachment_key = attachment.get("attachment_key") or ""
    pdf_sha1 = metadata.get("pdf_sha1") or ""

    return {
        "paper_id": paper_dir.name,
        "output_dir": str(paper_dir),
        "title": title,
        "date": date_text,
        "doi": doi,
        "attachment_key": attachment_key,
        "pdf_sha1": pdf_sha1,
        "title_year_key": title_year_key(title, date_text),
        "complete": extraction_complete(paper_dir),
    }


def prefer_existing_record(current: Optional[dict], candidate: dict) -> dict:
    if current is None:
        return candidate
    if candidate.get("complete") and not current.get("complete"):
        return candidate
    return current


def build_existing_extraction_index(output_root: Path) -> dict:
    index = {
        "by_attachment_key": {},
        "by_pdf_sha1": {},
        "by_doi": {},
        "by_title_year": {},
        "records": [],
    }

    if not output_root.exists():
        return index

    for paper_dir in sorted(output_root.iterdir()):
        if not paper_dir.is_dir():
            continue

        record = record_from_existing_dir(paper_dir)
        if not record:
            continue

        index["records"].append(record)

        if record.get("attachment_key"):
            key = record["attachment_key"]
            index["by_attachment_key"][key] = prefer_existing_record(index["by_attachment_key"].get(key), record)

        if record.get("pdf_sha1"):
            key = record["pdf_sha1"]
            index["by_pdf_sha1"][key] = prefer_existing_record(index["by_pdf_sha1"].get(key), record)

        if record.get("doi"):
            key = record["doi"]
            index["by_doi"][key] = prefer_existing_record(index["by_doi"].get(key), record)

        if record.get("title_year_key"):
            key = record["title_year_key"]
            index["by_title_year"][key] = prefer_existing_record(index["by_title_year"].get(key), record)

    return index


def find_existing_extraction(
    item_metadata: dict,
    attachment: dict,
    pdf_sha1: str,
    existing_index: dict,
) -> Tuple[Optional[dict], str]:
    attachment_key = attachment.get("attachment_key") or ""
    if attachment_key and attachment_key in existing_index.get("by_attachment_key", {}):
        return existing_index["by_attachment_key"][attachment_key], "attachment_key"

    if pdf_sha1 and pdf_sha1 in existing_index.get("by_pdf_sha1", {}):
        return existing_index["by_pdf_sha1"][pdf_sha1], "pdf_sha1"

    doi = normalize_doi(item_metadata.get("doi") or "")
    if doi and doi in existing_index.get("by_doi", {}):
        return existing_index["by_doi"][doi], "doi"

    ty_key = title_year_key(item_metadata.get("title") or "", item_metadata.get("date") or "")
    if ty_key and ty_key in existing_index.get("by_title_year", {}):
        return existing_index["by_title_year"][ty_key], "title_year"

    return None, ""


def write_metadata_if_safe(metadata_json_path: Path, metadata: dict, update_metadata: bool) -> None:
    if metadata_json_path.exists() and not update_metadata:
        return
    metadata_json_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")


def process_pdf(
    item_metadata: dict,
    attachment: dict,
    output_root: Path,
    grobid_url: str,
    copy_pdf: bool,
    skip_existing: bool,
    existing_index: dict,
    update_metadata: bool,
) -> dict:
    pdf_path = Path(attachment["pdf_path"])
    if not pdf_path.exists():
        return {
            "status": "missing_pdf_path",
            "skip_reason": "",
            "matched_existing_by": "",
            "existing_output_dir": "",
            "pdf_path": str(pdf_path),
            "error": "PDF path does not exist",
        }

    pdf_hash = sha1_file(pdf_path)
    existing_record, matched_by = find_existing_extraction(
        item_metadata=item_metadata,
        attachment=attachment,
        pdf_sha1=pdf_hash,
        existing_index=existing_index,
    )

    if skip_existing and existing_record and existing_record.get("complete"):
        return {
            "status": "skipped_existing_extraction",
            "skip_reason": "complete extraction already exists",
            "matched_existing_by": matched_by,
            "existing_output_dir": existing_record.get("output_dir", ""),
            "paper_id": existing_record.get("paper_id", ""),
            "zotero_key": item_metadata.get("zotero_key", ""),
            "attachment_key": attachment.get("attachment_key", ""),
            "title": item_metadata.get("title", ""),
            "doi": item_metadata.get("doi", ""),
            "date": item_metadata.get("date", ""),
            "pdf_path": str(pdf_path),
            "pdf_sha1": pdf_hash,
            "output_dir": existing_record.get("output_dir", ""),
            "grobid_status": "skipped_existing",
            "pymupdf_status": "skipped_existing",
            "pdfplumber_status": "skipped_existing",
            "error": "",
        }

    if existing_record and existing_record.get("output_dir"):
        paper_dir = Path(existing_record["output_dir"])
        paper_id = paper_dir.name
    else:
        paper_id = generated_paper_id(item_metadata, attachment, pdf_path)
        paper_dir = output_root / paper_id

    ensure_dir(paper_dir)
    paths = component_paths(paper_dir)

    metadata = {
        "item": item_metadata,
        "attachment": attachment,
        "pdf_sha1": pdf_hash,
        "source_pdf_path": str(pdf_path),
        "output_dir": str(paper_dir),
    }

    write_metadata_if_safe(paths["metadata"], metadata, update_metadata=update_metadata)

    if copy_pdf:
        copied_pdf = paper_dir / "source.pdf"
        if not copied_pdf.exists() or not skip_existing:
            shutil.copy2(pdf_path, copied_pdf)

    status = {
        "status": "processed",
        "skip_reason": "",
        "matched_existing_by": matched_by,
        "existing_output_dir": existing_record.get("output_dir", "") if existing_record else "",
        "paper_id": paper_id,
        "zotero_key": item_metadata.get("zotero_key", ""),
        "attachment_key": attachment.get("attachment_key", ""),
        "title": item_metadata.get("title", ""),
        "doi": item_metadata.get("doi", ""),
        "date": item_metadata.get("date", ""),
        "pdf_path": str(pdf_path),
        "pdf_sha1": pdf_hash,
        "output_dir": str(paper_dir),
        "grobid_status": "",
        "pymupdf_status": "",
        "pdfplumber_status": "",
        "error": "",
    }

    if skip_existing and grobid_component_complete(paper_dir):
        status["grobid_status"] = "skipped_existing"
    else:
        tei_xml, error = grobid_process_fulltext(pdf_path, grobid_url)

        if tei_xml:
            paths["tei"].write_text(tei_xml, encoding="utf-8")
            parsed = extract_grobid_metadata_and_sections(tei_xml)
            paths["parsed"].write_text(json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
            write_plain_text_from_grobid(parsed, paths["text"])
            status["grobid_status"] = "ok"
        else:
            status["grobid_status"] = "failed"
            status["error"] = error or "GROBID failed"

    if skip_existing and pymupdf_component_complete(paper_dir):
        status["pymupdf_status"] = "skipped_existing"
    else:
        pages_images = extract_pages_and_images_with_pymupdf(pdf_path, paper_dir)
        paths["pages"].write_text(
            json.dumps({"pages": pages_images.get("pages", [])}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        paths["images"].write_text(
            json.dumps({"images": pages_images.get("images", []), "error": pages_images.get("error", "")}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        status["pymupdf_status"] = "ok" if not pages_images.get("error") else "warning"

    if skip_existing and pdfplumber_component_complete(paper_dir):
        status["pdfplumber_status"] = "skipped_existing"
    else:
        tables = extract_tables_with_pdfplumber(pdf_path, paper_dir)
        paths["tables"].write_text(json.dumps(tables, indent=2, ensure_ascii=False), encoding="utf-8")
        status["pdfplumber_status"] = "ok" if not tables.get("error") else "warning"

    if (
        status["grobid_status"] == "skipped_existing"
        and status["pymupdf_status"] == "skipped_existing"
        and status["pdfplumber_status"] == "skipped_existing"
    ):
        status["status"] = "resumed_but_nothing_to_do"
        status["skip_reason"] = "all extraction components already exist"

    return status


def write_manifest_row(manifest_path: Path, row: dict) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not manifest_path.exists() or manifest_path.stat().st_size == 0

    with manifest_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDNAMES, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def collect_parent_items(collection_key_value: str, include_subcollections: bool) -> List[dict]:
    collection_keys = [collection_key_value]
    if include_subcollections:
        collection_keys = get_descendant_collection_keys(collection_key_value)

    seen_item_keys = set()
    parent_items = []

    for key in collection_keys:
        items = get_collection_items(key)
        for item in items:
            item_key_value = item.get("key") or item_data(item).get("key") or ""
            if not item_key_value or item_key_value in seen_item_keys:
                continue
            if not is_regular_item(item):
                continue
            seen_item_keys.add(item_key_value)
            parent_items.append(item)

    return parent_items


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Append-safe extraction of Zotero PDF attachments using Zotero local API + Dockerized GROBID."
    )

    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION_NAME,
        help=f"Zotero collection name, collection path, or collection key. Default: {DEFAULT_COLLECTION_NAME}",
    )

    parser.add_argument(
        "--outdir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output extraction database directory. Default: {DEFAULT_OUTPUT_DIR}",
    )

    parser.add_argument(
        "--grobid-url",
        default=GROBID_BASE_URL,
        help="GROBID base URL. Default: http://localhost:8070",
    )

    parser.add_argument(
        "--copy-pdf",
        action="store_true",
        help="Copy each source PDF into its output folder as source.pdf. Existing source.pdf files are preserved unless --no-skip-existing is used.",
    )

    parser.add_argument(
        "--no-skip-existing",
        action="store_true",
        help="Reprocess PDFs even if output files already exist. This can overwrite extraction component files.",
    )

    parser.add_argument(
        "--update-metadata",
        action="store_true",
        help="Update metadata.json in existing extraction folders. By default, existing metadata.json files are preserved.",
    )

    parser.add_argument(
        "--include-subcollections",
        action="store_true",
        help="Also extract PDFs from subcollections under the selected Zotero collection.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional maximum number of parent Zotero items to process. 0 means no limit.",
    )

    parser.add_argument(
        "--run-id",
        default="",
        help="Optional run ID used in manifest filenames. Default: current timestamp.",
    )

    args = parser.parse_args()

    output_root = Path(args.outdir)
    ensure_dir(output_root)

    run_id = args.run_id or timestamp_for_filename()
    run_manifest_path = output_root / f"manifest_{run_id}.csv"
    history_manifest_path = output_root / "manifest_history.csv"

    print("Checking Zotero local API...")
    check_zotero()

    print("Checking GROBID service...")
    check_grobid(args.grobid_url)

    collection_key_value, collection_path = find_collection(args.collection)
    print(f"Using collection: {collection_path}")
    print(f"Collection key: {collection_key_value}")

    print(f"Scanning existing extraction database: {output_root}")
    existing_index = build_existing_extraction_index(output_root)
    complete_existing_count = sum(1 for record in existing_index.get("records", []) if record.get("complete"))
    print(f"Existing extraction folders indexed: {len(existing_index.get('records', []))}")
    print(f"Complete extraction folders indexed: {complete_existing_count}")

    parent_items = collect_parent_items(
        collection_key_value=collection_key_value,
        include_subcollections=args.include_subcollections,
    )

    if args.limit and args.limit > 0:
        parent_items = parent_items[:args.limit]

    print(f"Found {len(parent_items)} parent items to check.")
    print(f"Run manifest: {run_manifest_path}")
    print(f"History manifest: {history_manifest_path}")

    processed_pdf_count = 0
    skipped_existing_count = 0
    missing_pdf_count = 0
    error_count = 0

    for index, item in enumerate(parent_items, start=1):
        item_meta = extract_metadata_from_item(item)
        title = item_meta.get("title") or "(untitled)"
        item_key_value = item_meta.get("zotero_key")

        print(f"\n[{index}/{len(parent_items)}] {title}")

        try:
            attachments = get_pdf_attachments_for_item(item_key_value)
        except Exception as exc:
            row = {
                "run_id": run_id,
                "status": "attachment_lookup_failed",
                "skip_reason": "",
                "matched_existing_by": "",
                "existing_output_dir": "",
                "paper_id": "",
                "zotero_key": item_key_value,
                "attachment_key": "",
                "title": title,
                "doi": item_meta.get("doi", ""),
                "date": item_meta.get("date", ""),
                "pdf_path": "",
                "pdf_sha1": "",
                "output_dir": "",
                "grobid_status": "",
                "pymupdf_status": "",
                "pdfplumber_status": "",
                "error": f"attachment_lookup_failed: {exc}",
            }
            write_manifest_row(run_manifest_path, row)
            write_manifest_row(history_manifest_path, row)
            error_count += 1
            continue

        usable_attachments = [a for a in attachments if a.get("pdf_path")]

        if not usable_attachments:
            print("  No usable local PDF attachment found.")
            missing_pdf_count += 1
            row = {
                "run_id": run_id,
                "status": "missing_pdf_path",
                "skip_reason": "no usable local PDF attachment",
                "matched_existing_by": "",
                "existing_output_dir": "",
                "paper_id": "",
                "zotero_key": item_key_value,
                "attachment_key": "",
                "title": title,
                "doi": item_meta.get("doi", ""),
                "date": item_meta.get("date", ""),
                "pdf_path": "",
                "pdf_sha1": "",
                "output_dir": "",
                "grobid_status": "",
                "pymupdf_status": "",
                "pdfplumber_status": "",
                "error": "no_usable_local_pdf_attachment",
            }
            write_manifest_row(run_manifest_path, row)
            write_manifest_row(history_manifest_path, row)
            continue

        for attachment in usable_attachments:
            print(f"  Checking PDF attachment: {attachment.get('attachment_title') or attachment.get('attachment_filename') or attachment.get('attachment_key')}")

            try:
                row = process_pdf(
                    item_metadata=item_meta,
                    attachment=attachment,
                    output_root=output_root,
                    grobid_url=args.grobid_url,
                    copy_pdf=args.copy_pdf,
                    skip_existing=not args.no_skip_existing,
                    existing_index=existing_index,
                    update_metadata=args.update_metadata,
                )
            except Exception as exc:
                row = {
                    "status": "error",
                    "skip_reason": "",
                    "matched_existing_by": "",
                    "existing_output_dir": "",
                    "paper_id": "",
                    "zotero_key": item_key_value,
                    "attachment_key": attachment.get("attachment_key", ""),
                    "title": title,
                    "doi": item_meta.get("doi", ""),
                    "date": item_meta.get("date", ""),
                    "pdf_path": attachment.get("pdf_path", ""),
                    "pdf_sha1": "",
                    "output_dir": "",
                    "grobid_status": "failed",
                    "pymupdf_status": "",
                    "pdfplumber_status": "",
                    "error": str(exc),
                }

            row["run_id"] = run_id
            write_manifest_row(run_manifest_path, row)
            write_manifest_row(history_manifest_path, row)

            if row.get("status") == "skipped_existing_extraction":
                skipped_existing_count += 1
            elif row.get("status") == "error" or row.get("error"):
                error_count += 1
                processed_pdf_count += 1
            else:
                processed_pdf_count += 1

            print(f"    Status: {row.get('status')}")
            print(f"    GROBID: {row.get('grobid_status')}")
            print(f"    PyMuPDF: {row.get('pymupdf_status')}")
            print(f"    pdfplumber: {row.get('pdfplumber_status')}")
            print(f"    Output: {row.get('output_dir')}")
            if row.get("matched_existing_by"):
                print(f"    Matched existing by: {row.get('matched_existing_by')}")
            if row.get("error"):
                print(f"    Error: {row.get('error')}")

    print("\nDone.")
    print(f"Processed PDF attachments: {processed_pdf_count}")
    print(f"Skipped already-complete extractions: {skipped_existing_count}")
    print(f"Items without usable local PDFs: {missing_pdf_count}")
    print(f"Errors/warnings with error text: {error_count}")
    print(f"Run manifest: {run_manifest_path}")
    print(f"History manifest: {history_manifest_path}")


if __name__ == "__main__":
    main()
