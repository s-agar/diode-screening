#!/usr/bin/env python3

import argparse
import csv
import html
import json
import re
import shutil
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


DEFAULT_INPUT_DIR = "extracted_zotero_grobid"
DEFAULT_OUTPUT_DIR = "evidence_detection"


EDGE_TERMS = {
    "JTE": [
        "junction termination extension",
        "junction-termination extension",
        "termination extension",
        "JTE",
        "double-zone JTE",
        "multi-zone JTE",
        "space-modulated JTE",
        "implanted JTE",
        "floating JTE",
    ],
    "field_limiting_rings": [
        "field limiting ring",
        "field-limiting ring",
        "field limiting rings",
        "field-limiting rings",
        "floating field ring",
        "floating-field ring",
        "floating field rings",
        "floating-field rings",
        "FLR",
        "FFR",
    ],
    "guard_ring": [
        "guard ring",
        "guard-ring",
        "guard rings",
        "guard-rings",
        "ring assisted",
        "ring-assisted",
    ],
    "field_plate": [
        "field plate",
        "field-plate",
        "metal field plate",
        "metal field-plate",
        "oxide field plate",
        "oxide field-plate",
    ],
    "mesa": [
        "mesa",
        "mesa termination",
        "mesa edge termination",
        "mesa edge-termination",
        "etched mesa",
    ],
    "bevel": [
        "bevel",
        "beveled",
        "bevelled",
        "bevel termination",
        "beveled mesa",
        "bevelled mesa",
        "beveled mesa termination",
        "bevelled mesa termination",
        "negative bevel",
        "positive bevel",
        "beveled edge termination",
        "bevelled edge termination",
    ],
    "trench": [
        "trench termination",
        "trench edge termination",
        "trench edge-termination",
    ],
    "generic_edge_termination": [
        "edge termination",
        "edge-termination",
        "peripheral termination",
        "termination structure",
        "termination region",
        "termination design",
        "termination efficiency",
        "termination extension",
    ],
}


DEVICE_TERMS = [
    "SiC",
    "4H-SiC",
    "6H-SiC",
    "silicon carbide",
    "diode",
    "diodes",
    "Schottky",
    "SBD",
    "JBS diode",
    "JBS diodes",
    "junction barrier Schottky diode",
    "junction barrier Schottky diodes",
    "PiN diode",
    "PiN diodes",
    "PIN diode",
    "PIN diodes",
    "p-i-n diode",
    "p-i-n diodes",
    "PN diode",
    "PN diodes",
    "p-n diode",
    "p-n diodes",
    "MPS diode",
    "MPS diodes",
    "merged PiN Schottky diode",
    "merged PiN Schottky diodes",
    "rectifier",
    "rectifiers",
]


DIODE_DEVICE_PATTERNS = [
    (
        "JBS diode",
        r"\bJBS\s+(?:diode|diodes|rectifier|rectifiers)\b|"
        r"\bjunction barrier schottky\s+(?:diode|diodes|rectifier|rectifiers)\b",
    ),
    (
        "Schottky barrier diode",
        r"\bSchottky barrier diode\b|"
        r"\bSchottky barrier diodes\b|"
        r"\bSBDs?\b|"
        r"\bSchottky diode\b|"
        r"\bSchottky diodes\b|"
        r"\bSchottky-barrier diode\b|"
        r"\bSchottky-barrier diodes\b|"
        r"\bSchottky rectifier\b|"
        r"\bSchottky rectifiers\b",
    ),
    (
        "PiN diode",
        r"\bPiN\s+(?:diode|diodes|rectifier|rectifiers)\b|"
        r"\bPIN\s+(?:diode|diodes|rectifier|rectifiers)\b|"
        r"\bp-i-n\s+(?:diode|diodes|rectifier|rectifiers)\b",
    ),
    (
        "PN diode",
        r"\bPN\s+(?:diode|diodes|rectifier|rectifiers)\b|"
        r"\bp-n\s+(?:diode|diodes|rectifier|rectifiers)\b",
    ),
    (
        "MPS diode",
        r"\bMPS\s+(?:diode|diodes|rectifier|rectifiers)\b|"
        r"\bmerged PiN Schottky\s+(?:diode|diodes|rectifier|rectifiers)\b|"
        r"\bmerged pin schottky\s+(?:diode|diodes|rectifier|rectifiers)\b",
    ),
    ("rectifier", r"\brectifier\b|\brectifiers\b"),
    ("diode", r"\bdiode\b|\bdiodes\b"),
]


TRANSISTOR_TITLE_TERMS = [
    "transistor",
    "transistors",
    "MOSFET",
    "MOSFETs",
    "UMOSFET",
    "UMOSFETs",
    "U-MOSFET",
    "U-MOSFETs",
    "JFET",
    "JFETs",
    "MESFET",
    "MESFETs",
    "HEMT",
    "HEMTs",
    "FET",
    "FETs",
    "field-effect transistor",
    "field effect transistor",
    "field-effect transistors",
    "field effect transistors",
    "JBSFET",
    "JBSFETs",
    "VDMOSFET",
    "VDMOSFETs",
    "MISFET",
    "MISFETs",
    "FinFET",
    "FinFETs",
    "IGBT",
    "IGBTs",
    "IGTBT",
    "IGTBTs",
    "VJFET",
    "VJFETs",
    "BiDFET",
    "BiDFETs",
    "DMOSFET",
    "DMOSFETs",
    "BJT",
    "BJTs",
    "bipolar transistor",
    "bipolar transistors",
    "bipolar junction transistor",
    "bipolar junction transistors",
    "bipolar-junction transistor",
    "bipolar-junction transistors",
]


DIODE_OR_DEVICE_TITLE_RESCUE_TERMS = [
    "diode",
    "diodes",
    "device",
    "devices",
    "rectifier",
    "rectifiers",
    "Schottky diode",
    "Schottky diodes",
    "Schottky rectifier",
    "Schottky rectifiers",
    "Schottky barrier diode",
    "Schottky barrier diodes",
    "SBD",
    "SBDs",
    "JBS diode",
    "JBS diodes",
    "JBS rectifier",
    "JBS rectifiers",
    "junction barrier Schottky diode",
    "junction barrier Schottky diodes",
    "junction barrier Schottky rectifier",
    "junction barrier Schottky rectifiers",
    "PiN diode",
    "PiN diodes",
    "PIN diode",
    "PIN diodes",
    "p-i-n diode",
    "p-i-n diodes",
    "PN diode",
    "PN diodes",
    "p-n diode",
    "p-n diodes",
    "MPS diode",
    "MPS diodes",
    "MPS rectifier",
    "MPS rectifiers",
    "merged PiN Schottky diode",
    "merged PiN Schottky diodes",
]


BREAKDOWN_KEYWORDS = [
    "breakdown voltage",
    "blocking voltage",
    "reverse blocking voltage",
    "breakdown",
    "blocking",
    "Vbr",
    "V_br",
    "BV",
    "VB",
    "V_B",
]


RONSP_KEYWORDS = [
    "specific on-resistance",
    "specific ON-resistance",
    "specific on resistance",
    "specific ON resistance",
    "specific differential on-resistance",
    "differential specific on-resistance",
    "on-resistance",
    "ON-resistance",
    "on resistance",
    "ON resistance",
    "onresistance",
    "ONresistance",
    "onstate resistance",
    "ON-state resistance",
    "on-state resistance",
    "specific onstate resistance",
    "specific ON-state resistance",
    "differential specific onstate resistance",
    "differential specific ON-state resistance",
    "Ron,sp",
    "R_on,sp",
    "Ronsp",
    "RONA",
    "R on,sp",
    "BV2/Ron,sp",
    "BV^2/Ron,sp",
]


SIMULATION_WORDS = [
    "simulated",
    "simulation",
    "calculated",
    "modeled",
    "modelled",
    "designed",
    "predicted",
    "theoretical",
    "tcad",
]


MEASUREMENT_WORDS = [
    "measured",
    "fabricated",
    "experimental",
    "experimentally",
    "forward i-v",
    "reverse i-v",
    "i-v characteristics",
    "leakage current",
    "at a leakage",
    "room temperature",
]


COMPARISON_RISK_WORDS = [
    "reported by",
    "previously reported",
    "literature",
    "comparison",
    "compared with",
    "state-of-the-art",
    "state of the art",
    "benchmark",
    "reference",
    "references",
    "refs.",
    "ref.",
]


REFERENCE_HEADING_PATTERNS = [
    r"(?im)^\s*references\s*$",
    r"(?im)^\s*reference\s*$",
    r"(?im)^\s*bibliography\s*$",
]


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def timestamp_for_filename() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


class TimingLogger:
    def __init__(self, path: Path, enabled: bool = True, live_enabled: bool = False) -> None:
        self.path = path
        self.enabled = enabled
        self.live_enabled = live_enabled
        self.run_start = time.perf_counter()
        self._open_handles = {}
        self._fh = None

        if self.enabled:
            ensure_dir(path.parent)
            self._fh = path.open("w", encoding="utf-8")

    def close(self) -> None:
        if self._fh is not None:
            self._fh.flush()
            self._fh.close()
            self._fh = None

    def _now_record(self, event: str, paper_id: str = "", stage: str = "", **extra) -> dict:
        record = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "run_elapsed_s": round(time.perf_counter() - self.run_start, 6),
            "event": event,
            "paper_id": paper_id,
            "stage": stage,
        }
        record.update(extra)
        return record

    def log(self, event: str, paper_id: str = "", stage: str = "", live: bool = True, **extra) -> None:
        record = self._now_record(event=event, paper_id=paper_id, stage=stage, **extra)

        if self.enabled and self._fh is not None:
            self._fh.write(json.dumps(record, ensure_ascii=False))
            self._fh.write("\n")
            self._fh.flush()

        if live and self.live_enabled:
            detail_parts = []
            for key in [
                "paper_index",
                "paper_total",
                "source_index",
                "source_total",
                "source_type",
                "source_id",
                "chars",
                "matches",
                "elapsed_s",
                "decision",
            ]:
                if key in extra and extra[key] not in [None, ""]:
                    detail_parts.append(f"{key}={extra[key]}")
            details = " | " + "; ".join(detail_parts) if detail_parts else ""
            paper_text = f" | paper={paper_id}" if paper_id else ""
            stage_text = f" | stage={stage}" if stage else ""
            print(
                f"[timing {record['timestamp']} +{record['run_elapsed_s']:.2f}s] "
                f"{event}{paper_text}{stage_text}{details}",
                flush=True,
            )

    def start(self, key: str, paper_id: str = "", stage: str = "", **extra) -> None:
        self._open_handles[key] = time.perf_counter()
        self.log("start", paper_id=paper_id, stage=stage, **extra)

    def end(self, key: str, paper_id: str = "", stage: str = "", **extra) -> float:
        start_time = self._open_handles.pop(key, None)
        elapsed = 0.0 if start_time is None else time.perf_counter() - start_time
        extra.setdefault("elapsed_s", round(elapsed, 6))
        self.log("end", paper_id=paper_id, stage=stage, **extra)
        return elapsed


def safe_read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def safe_read_json(path: Path) -> object:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def clean_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_doi(doi: str) -> str:
    doi = (doi or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = doi.strip().strip(".,;:)]}")
    return doi


def normalize_path_key(path_text: str) -> str:
    if not path_text:
        return ""
    try:
        return str(Path(path_text).expanduser().resolve()).lower()
    except Exception:
        return str(path_text).strip().lower()


def result_identity_keys_from_metadata(metadata: dict) -> set:
    keys = set()

    paper_id = metadata.get("paper_id") or ""
    if paper_id:
        keys.add(f"paper_id:{paper_id}")

    attachment_key = metadata.get("attachment_key") or ""
    if attachment_key:
        keys.add(f"attachment_key:{attachment_key}")

    doi = normalize_doi(metadata.get("doi") or "")
    if doi:
        keys.add(f"doi:{doi}")

    source_pdf_path = normalize_path_key(metadata.get("source_pdf_path") or "")
    if source_pdf_path:
        keys.add(f"source_pdf_path:{source_pdf_path}")

    output_dir = normalize_path_key(metadata.get("output_dir") or "")
    if output_dir:
        keys.add(f"output_dir:{output_dir}")

    pdf_sha1 = metadata.get("pdf_sha1") or ""
    if pdf_sha1:
        keys.add(f"pdf_sha1:{pdf_sha1}")

    return keys


def result_identity_keys(result: dict) -> set:
    metadata = result.get("metadata") or {}
    if not isinstance(metadata, dict):
        return set()
    return result_identity_keys_from_metadata(metadata)


def load_existing_results(path: Path) -> List[dict]:
    if not path.exists():
        return []

    results = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_number, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"Warning: could not parse existing JSONL line {line_number}; ignoring it.")
                continue
            if isinstance(record, dict):
                results.append(record)
    return results


def build_existing_identity_index(results: List[dict]) -> set:
    keys = set()
    for result in results:
        keys.update(result_identity_keys(result))
    return keys


def is_already_processed(metadata: dict, existing_keys: set) -> bool:
    if not existing_keys:
        return False
    return bool(result_identity_keys_from_metadata(metadata) & existing_keys)


def strip_html_tags(text: str) -> str:
    text = text or ""
    text = html.unescape(text)
    text = re.sub(r"<\s*sup\s*>\s*2\s*<\s*/\s*sup\s*>", "2", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*sup\s*>\s*-\s*2\s*<\s*/\s*sup\s*>", "-2", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def repair_linebreak_hyphenation(text: str) -> str:
    text = text or ""
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text)
    text = re.sub(r"(?<=\w)-\s+(?=\w)", "", text)
    return text


def normalize_legacy_pdf_symbol_artifacts(text: str) -> str:
    """Normalize common legacy PDF/Symbol-font extraction artifacts.

    Several older IEEE-style PDFs encode mathematical symbols using legacy
    fonts. Depending on the extractor, the same visual text can emerge as
    Latin-1 or ASCII lookalikes rather than Unicode symbols, for example:

      mΩ · cm²  ->  mO Á cm 2
      mΩ cm²    ->  mX cm 2
      cm⁻²      ->  cm À2

    This function repairs those artifacts in a general unit-decoding pass
    before the field detectors run. It is intentionally context-limited to
    electrical-unit shapes so that ordinary words containing O/X/Q are not
    rewritten.
    """
    text = text or ""

    # In this corpus, À often represents a minus sign and Á often represents
    # a multiplication dot or separator. Preserve minus when it precedes a
    # digit, otherwise use whitespace as a safe separator.
    text = re.sub(r"[Àà]\s*(?=\d)", "-", text)
    text = text.replace("À", " ").replace("à", " ")
    text = text.replace("Á", " ").replace("á", " ")

    # Normalize known multiplication-dot variants before unit repair.
    text = text.replace("⋅", " ")
    text = text.replace("·", " ")
    text = text.replace("•", " ")
    text = text.replace("∙", " ")
    text = text.replace("*", " ")

    # Some extractors decode Ω from legacy Symbol fonts as O, X, Q, or 0.
    # Treat those as ohm only when they appear as the resistance-area unit
    # immediately before cm2/cm 2/cm-2. This fixes forms such as
    # "mO cm 2", "mX cm 2", "mQ-cm2", and "m0/cm2" without
    # globally rewriting the letter X or O.
    # Keep separators short and non-nested. A previous version used a nested
    # optional separator pattern that could catastrophically backtrack on sparse
    # pdfplumber CSV fragments such as "I\nR,M,,,,". Unit corruption repairs
    # only need to bridge a few spaces/punctuation characters between m/O/Ω
    # and cm2, so a bounded separator is both safer and more precise.
    sep = r"[ \t\r\f\v\-:/\\.,;]{0,8}"
    cm2_lookahead = r"(?=c[ \t\r\f\v]{0,3}m[ \t\r\f\v]{0,3}(?:\^?[ \t\r\f\v]{0,3}2|-[ \t\r\f\v]{0,3}2)|cm2)"
    text = re.sub(
        r"\bm[ \t\r\f\v]{0,3}(?:ohms?|[oO0qQxXΩΩω])" + sep + cm2_lookahead,
        "mohm ",
        text,
        flags=re.IGNORECASE,
    )

    # If the ohm glyph vanished completely, the unit can become "m cm 2".
    # Keep this repair context-limited and bounded to avoid treating unrelated
    # isolated M characters in tables as resistance units.
    text = re.sub(r"\bm" + sep + cm2_lookahead, "mohm ", text, flags=re.IGNORECASE)

    return text


def normalize_for_search(text: str) -> str:
    text = text or ""
    text = strip_html_tags(text)
    text = repair_linebreak_hyphenation(text)
    text = unicodedata.normalize("NFKC", text)

    text = text.replace("/spl Omega/", "ohm")
    text = text.replace("/spl omega/", "ohm")
    text = text.replace("/spl middot/", " ")
    text = text.replace("/spl mu/", "u")
    text = text.replace("/sup 2/", "2")
    text = text.replace("/sub ", "_")

    text = text.replace("−", "-")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("‐", "-")

    text = normalize_legacy_pdf_symbol_artifacts(text)
    text = text.replace("×", "x")

    text = text.replace("Ω", "ohm")
    text = text.replace("Ω", "ohm")
    text = text.replace("ω", "ohm")
    text = text.replace("μ", "u")
    text = text.replace("µ", "u")

    text = text.replace("²", "2")
    text = text.replace("^2", "2")

    # Some PDF extractors duplicate italic unit letters, e.g. original
    # "mΩ cm2" can become "𝑚𝑚Ω 𝑐𝑐𝑚𝑚 2" -> NFKC "mmΩ ccmm 2".
    # Normalize only the unit-shaped artifacts rather than doubling all letters.
    text = re.sub(r"\bm\s*m\s*ohms?\b", "mohm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bmmohms?\b", "mohm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bc\s*c\s*m\s*m\s*\^?\s*2\b", "cm2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bc\s*m\s*m\s*\^?\s*2\b", "cm2", text, flags=re.IGNORECASE)

    text = re.sub(r"\bm\s*ohm\b", "mohm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bm\s*-\s*ohm\b", "mohm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bm\s*ohms\b", "mohm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bm\s*Ω\b", "mohm", text, flags=re.IGNORECASE)
    text = re.sub(r"\bm\s*Ω\b", "mohm", text, flags=re.IGNORECASE)

    text = re.sub(r"\b(mohm|ohm)\s*[-‐-‒–—−•·×]?\s*cm\s*\^?\s*2\b", r"\1 cm2", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcm\s*\^?\s*2\b", "cm2", text, flags=re.IGNORECASE)

    return text


def trim_text_before_references(text: str) -> str:
    if not text:
        return ""

    earliest = None
    for pattern in REFERENCE_HEADING_PATTERNS:
        match = re.search(pattern, text)
        if match:
            if earliest is None or match.start() < earliest:
                earliest = match.start()

    if earliest is not None:
        return text[:earliest]

    return text


def is_reference_heading(heading: str) -> bool:
    heading_l = clean_space(heading).lower()
    return heading_l in {"references", "reference", "bibliography"}


def is_reference_like_snippet(snippet: str) -> bool:
    s = clean_space(normalize_for_search(snippet))
    s_l = s.lower()

    if not s:
        return False

    citation_markers = [
        "ieee electron device lett",
        "ieee trans",
        "appl. phys.",
        "j. appl. phys",
        "phys. status solidi",
        "mater. sci. forum",
        "proc.",
        "symp.",
        "conference",
        "doi:",
        "vol.",
        "no.",
        "pp.",
        "et al.",
    ]

    marker_count = sum(1 for marker in citation_markers if marker in s_l)
    bracket_citations = len(re.findall(r"\[\d+\]", s))
    year_count = len(re.findall(r"\b(?:19|20)\d{2}\b", s))
    title_like_quotes = len(re.findall(r"[\"“”]", s))

    if marker_count >= 2 and year_count >= 1:
        return True

    if bracket_citations >= 2 and year_count >= 1 and marker_count >= 1:
        return True

    if year_count >= 3 and marker_count >= 2:
        return True

    if "references" in s_l[:80] and year_count >= 1:
        return True

    if title_like_quotes >= 2 and year_count >= 2 and marker_count >= 1:
        return True

    return False


def context_window(normalized_text: str, start: int, end: int, window: int = 350) -> str:
    """Return a compact context window from text that is already normalized.

    The detection functions call normalize_for_search() once per source and pass
    the resulting text through regex matching. Earlier versions normalized the
    full source again for every individual evidence match, which could make
    generic high-frequency terms such as mesa/termination/breakdown extremely
    slow on long papers.
    """
    normalized_text = normalized_text or ""
    left = max(0, start - window)
    right = min(len(normalized_text), end + window)
    snippet = normalized_text[left:right]
    return clean_space(snippet)


def phrase_pattern(phrase: str) -> re.Pattern:
    parts = re.split(r"[\s-]+", phrase.strip())
    escaped = [re.escape(part) for part in parts if part]
    if not escaped:
        return re.compile(r"a^")
    pattern = r"[\s\-]*".join(escaped)
    return re.compile(pattern, flags=re.IGNORECASE)


PHRASE_PATTERN_CACHE: Dict[str, re.Pattern] = {}


def compiled_phrase_pattern(phrase: str) -> re.Pattern:
    pattern = PHRASE_PATTERN_CACHE.get(phrase)
    if pattern is None:
        pattern = phrase_pattern(phrase)
        PHRASE_PATTERN_CACHE[phrase] = pattern
    return pattern


def compile_edge_term_patterns() -> Dict[str, List[Tuple[str, re.Pattern]]]:
    return {
        category: [(term, compiled_phrase_pattern(term)) for term in terms]
        for category, terms in EDGE_TERMS.items()
    }


def compile_device_type_patterns() -> List[Tuple[str, re.Pattern]]:
    return [
        (label, re.compile(pattern_text, flags=re.IGNORECASE))
        for label, pattern_text in DIODE_DEVICE_PATTERNS
    ]


EDGE_TERM_PATTERNS = compile_edge_term_patterns()
DEVICE_TYPE_PATTERNS = compile_device_type_patterns()


TITLE_TRANSISTOR_PATTERN = re.compile(
    r"(?i)\b("
    r"transistors?|"
    r"MOSFETs?|U[- ]?MOSFETs?|JFETs?|MESFETs?|HEMTs?|FETs?|"
    r"JBSFETs?|VDMOSFETs?|MISFETs?|FinFETs?|IGBTs?|IGTBTs?|"
    r"VJFETs?|BiDFETs?|DMOSFETs?|BJTs?|"
    r"bipolar[- ](?:junction[- ])?transistors?|"
    r"field[- ]effect transistors?"
    r")\b"
)


TITLE_DIODE_OR_DEVICE_RESCUE_PATTERN = re.compile(
    r"(?i)\b("
    r"diodes?|devices?|rectifiers?|"
    r"SBDs?|"
    r"Schottky(?: barrier)?\s+(?:diodes?|rectifiers?)|"
    r"JBS\s+(?:diodes?|rectifiers?)|"
    r"junction barrier schottky\s+(?:diodes?|rectifiers?)|"
    r"PiN\s+(?:diodes?|rectifiers?)|PIN\s+(?:diodes?|rectifiers?)|p-i-n\s+(?:diodes?|rectifiers?)|"
    r"PN\s+(?:diodes?|rectifiers?)|p-n\s+(?:diodes?|rectifiers?)|"
    r"MPS\s+(?:diodes?|rectifiers?)|"
    r"merged PiN Schottky\s+(?:diodes?|rectifiers?)"
    r")\b"
)


NUMBER_PATTERN = r"(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
VOLTAGE_UNIT_PATTERN = r"(?P<unit>kV|V)"
BV_KEYWORD_BEFORE_PATTERN = (
    r"(?P<keyword>V\s*br|V_br|V\s*B|V_B|BV|breakdown voltage|"
    r"blocking voltage|reverse blocking voltage|breakdown|blocking)"
)
BV_KEYWORD_AFTER_PATTERN = (
    r"(?P<keyword2>breakdown voltage|blocking voltage|reverse blocking voltage|breakdown|blocking)"
)
BV_PATTERNS_GENERAL = [
    re.compile(rf"(?i)\b{BV_KEYWORD_BEFORE_PATTERN}\b[^.;,\n]{{0,160}}?{NUMBER_PATTERN}\s*{VOLTAGE_UNIT_PATTERN}\b"),
    re.compile(rf"(?i)\b{NUMBER_PATTERN}\s*{VOLTAGE_UNIT_PATTERN}\b[^.;,\n]{{0,160}}?\b{BV_KEYWORD_AFTER_PATTERN}\b"),
]
BV_PATTERN_TITLE = re.compile(rf"(?i)\b{NUMBER_PATTERN}\s*{VOLTAGE_UNIT_PATTERN}\b")


RON_UNIT_PATTERN = r"(?P<unit>m\s*ohm|mohm|milliohm|ohm|ohms|m)"
RON_AREA_PATTERN = r"(?:cm\s*\^?\s*2|cm2|cm\s*-\s*2|cm\s*/?\s*sup\s*2)"
RON_KEYWORD_PATTERN_TEXT = (
    r"(?:"
    r"R\s*on\s*,?\s*sp|R_on\s*,?\s*sp|Ron\s*,?\s*sp|Ronsp|RONA|"
    r"R\s*on\s*sp|"
    r"specific\s+(?:ON|on)\s*[- ]?\s*(?:state\s*)?resistance|"
    r"specific\s+differential\s+(?:ON|on)\s*[- ]?\s*(?:state\s*)?resistance|"
    r"differential\s+specific\s+(?:ON|on)\s*[- ]?\s*(?:state\s*)?resistance|"
    r"specific\s+(?:ON|on)\s*state\s*resistance|"
    r"specific\s+resistance|"
    r"(?:ON|on)\s*[- ]?\s*(?:state\s*)?resistance|"
    r"(?:ON|on)resistance|"
    r"BV2\s*/\s*Ron\s*,?\s*sp|BV\^2\s*/\s*Ron\s*,?\s*sp"
    r")"
)
RON_KEYWORD_RE = re.compile(RON_KEYWORD_PATTERN_TEXT, flags=re.IGNORECASE)
RON_PATTERNS_GENERAL = [
    re.compile(
        rf"(?i)\b(?P<keyword>{RON_KEYWORD_PATTERN_TEXT})\b"
        rf"[^.;\n]{{0,260}}?"
        rf"{NUMBER_PATTERN}\s*[- ]?\s*{RON_UNIT_PATTERN}\s*[- ]?\s*{RON_AREA_PATTERN}"
    ),
    re.compile(
        rf"(?i)\b{NUMBER_PATTERN}\s*[- ]?\s*{RON_UNIT_PATTERN}\s*[- ]?\s*{RON_AREA_PATTERN}"
        rf"[^.;\n]{{0,260}}?"
        rf"\b(?P<keyword2>{RON_KEYWORD_PATTERN_TEXT})\b"
    ),
]
RON_PATTERN_TITLE = re.compile(rf"(?i)\b{NUMBER_PATTERN}\s*[- ]?\s*{RON_UNIT_PATTERN}\s*[- ]?\s*{RON_AREA_PATTERN}\b")


def term_found(text: str, term: str) -> bool:
    return bool(compiled_phrase_pattern(term).search(text))


def has_any(text: str, terms: List[str]) -> bool:
    return any(term_found(text, term) for term in terms)


def title_suppresses_device_type(title: str) -> bool:
    title_l = normalize_for_search(title or "")
    has_transistor_term = bool(TITLE_TRANSISTOR_PATTERN.search(title_l))
    has_diode_or_device_rescue = bool(TITLE_DIODE_OR_DEVICE_RESCUE_PATTERN.search(title_l))

    return has_transistor_term and not has_diode_or_device_rescue


def classify_context(snippet: str) -> Dict[str, bool]:
    snippet_l = normalize_for_search(snippet).lower()
    reference_context = is_reference_like_snippet(snippet)

    return {
        "simulation_context": any(word in snippet_l for word in SIMULATION_WORDS),
        "measurement_context": any(word in snippet_l for word in MEASUREMENT_WORDS),
        "comparison_risk": any(word in snippet_l for word in COMPARISON_RISK_WORDS) or reference_context,
        "reference_context": reference_context,
    }


def evidence_score_base(source_type: str) -> int:
    if source_type in {"metadata_title", "grobid_title"}:
        return 6
    if source_type == "grobid_abstract":
        return 5
    if source_type == "grobid_section":
        return 5
    if source_type == "grobid_figure":
        return 5
    if source_type == "grobid_table":
        return 5
    if source_type == "pdfplumber_table":
        return 4
    if source_type == "pymupdf_page":
        return 3
    if source_type == "fulltext":
        return 3
    return 1


def parse_metadata(paper_dir: Path) -> dict:
    metadata = safe_read_json(paper_dir / "metadata.json")
    if not isinstance(metadata, dict):
        return {}

    item = metadata.get("item") or {}
    attachment = metadata.get("attachment") or {}

    return {
        "paper_id": paper_dir.name,
        "title": item.get("title", ""),
        "doi": item.get("doi", ""),
        "zotero_key": item.get("zotero_key", ""),
        "attachment_key": attachment.get("attachment_key", ""),
        "source_pdf_path": metadata.get("source_pdf_path", ""),
        "output_dir": str(paper_dir),
        "pdf_sha1": metadata.get("pdf_sha1", ""),
    }


def collect_sources(
    paper_dir: Path,
    timing_logger: Optional[TimingLogger] = None,
    paper_id: str = "",
) -> List[dict]:
    sources = []
    metadata = parse_metadata(paper_dir)
    if not paper_id:
        paper_id = metadata.get("paper_id", paper_dir.name)

    def log_collect(event: str, step: str, **extra) -> None:
        if timing_logger:
            timing_logger.log(event, paper_id=paper_id, stage=f"collect_sources:{step}", **extra)

    log_collect("checkpoint", "metadata", title=metadata.get("title", ""))

    metadata_title = metadata.get("title", "")
    if metadata_title:
        sources.append({
            "source_type": "metadata_title",
            "source_id": "zotero_title",
            "page": "",
            "heading": "Title",
            "text": metadata_title,
        })

    log_collect("start", "read_grobid_parsed")
    parsed = safe_read_json(paper_dir / "grobid_parsed.json")
    log_collect("end", "read_grobid_parsed", parsed_is_dict=isinstance(parsed, dict))
    if isinstance(parsed, dict):
        grobid_title = parsed.get("title") or ""
        if grobid_title and grobid_title != metadata_title:
            sources.append({
                "source_type": "grobid_title",
                "source_id": "grobid_title",
                "page": "",
                "heading": "Title",
                "text": grobid_title,
            })

        if parsed.get("abstract"):
            sources.append({
                "source_type": "grobid_abstract",
                "source_id": "abstract",
                "page": "",
                "heading": "Abstract",
                "text": parsed.get("abstract", ""),
            })

        for i, section in enumerate(parsed.get("sections") or [], start=1):
            heading = section.get("heading") or f"section_{i}"
            text = section.get("text") or ""
            log_collect("checkpoint", "grobid_section", source_index=i, source_id=f"section_{i}", chars=len(text), heading=heading)

            if is_reference_heading(heading):
                continue

            trim_key = f"collect:{paper_id}:grobid_section:{i}:trim"
            if timing_logger:
                timing_logger.start(trim_key, paper_id=paper_id, stage="collect_sources:trim_references", source_index=i, source_id=f"section_{i}", chars=len(text))
            text = trim_text_before_references(text)
            if timing_logger:
                timing_logger.end(trim_key, paper_id=paper_id, stage="collect_sources:trim_references", source_index=i, source_id=f"section_{i}", chars=len(text))

            if text:
                sources.append({
                    "source_type": "grobid_section",
                    "source_id": f"section_{i}",
                    "page": "",
                    "heading": heading,
                    "text": text,
                })

        for i, fig in enumerate(parsed.get("figures") or [], start=1):
            text = " ".join([
                fig.get("label") or "",
                fig.get("head") or "",
                fig.get("caption") or "",
                fig.get("text") or "",
            ])
            log_collect("checkpoint", "grobid_figure", source_index=i, source_id=f"figure_{i}", chars=len(text))
            ref_key = f"collect:{paper_id}:grobid_figure:{i}:reference_check"
            if timing_logger:
                timing_logger.start(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_index=i, source_id=f"figure_{i}", chars=len(text))
            is_ref = is_reference_like_snippet(text)
            if timing_logger:
                timing_logger.end(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_index=i, source_id=f"figure_{i}", chars=len(text))
            if clean_space(text) and not is_ref:
                sources.append({
                    "source_type": "grobid_figure",
                    "source_id": f"figure_{i}",
                    "page": "",
                    "heading": fig.get("label") or fig.get("head") or f"figure_{i}",
                    "text": text,
                })

        for i, table in enumerate(parsed.get("tables") or [], start=1):
            text = " ".join([
                table.get("label") or "",
                table.get("head") or "",
                table.get("caption") or "",
                table.get("text") or "",
            ])
            log_collect("checkpoint", "grobid_table", source_index=i, source_id=f"grobid_table_{i}", chars=len(text))
            ref_key = f"collect:{paper_id}:grobid_table:{i}:reference_check"
            if timing_logger:
                timing_logger.start(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_index=i, source_id=f"grobid_table_{i}", chars=len(text))
            is_ref = is_reference_like_snippet(text)
            if timing_logger:
                timing_logger.end(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_index=i, source_id=f"grobid_table_{i}", chars=len(text))
            if clean_space(text) and not is_ref:
                sources.append({
                    "source_type": "grobid_table",
                    "source_id": f"grobid_table_{i}",
                    "page": "",
                    "heading": table.get("label") or table.get("head") or f"grobid_table_{i}",
                    "text": text,
                })

    log_collect("start", "read_fulltext")
    fulltext = safe_read_text(paper_dir / "fulltext.txt")
    log_collect("end", "read_fulltext", chars=len(fulltext))
    trim_key = f"collect:{paper_id}:fulltext:trim"
    if timing_logger:
        timing_logger.start(trim_key, paper_id=paper_id, stage="collect_sources:trim_references", source_id="fulltext.txt", chars=len(fulltext))
    fulltext = trim_text_before_references(fulltext)
    if timing_logger:
        timing_logger.end(trim_key, paper_id=paper_id, stage="collect_sources:trim_references", source_id="fulltext.txt", chars=len(fulltext))
    if fulltext:
        sources.append({
            "source_type": "fulltext",
            "source_id": "fulltext.txt",
            "page": "",
            "text": fulltext,
        })

    log_collect("start", "read_pages")
    pages = safe_read_json(paper_dir / "pages.json")
    log_collect("end", "read_pages", parsed_is_dict=isinstance(pages, dict))
    if isinstance(pages, dict):
        for page in pages.get("pages") or []:
            text = page.get("text") or ""
            page_number = page.get("page_number", "")
            log_collect("checkpoint", "pymupdf_page", source_id=f"page_{page_number}", chars=len(text))
            trim_key = f"collect:{paper_id}:page:{page_number}:trim"
            if timing_logger:
                timing_logger.start(trim_key, paper_id=paper_id, stage="collect_sources:trim_references", source_id=f"page_{page_number}", chars=len(text))
            text = trim_text_before_references(text)
            if timing_logger:
                timing_logger.end(trim_key, paper_id=paper_id, stage="collect_sources:trim_references", source_id=f"page_{page_number}", chars=len(text))
            ref_key = f"collect:{paper_id}:page:{page_number}:reference_check"
            if timing_logger:
                timing_logger.start(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_id=f"page_{page_number}", chars=min(len(text), 1500))
            is_ref = is_reference_like_snippet(text[:1500])
            if timing_logger:
                timing_logger.end(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_id=f"page_{page_number}", chars=min(len(text), 1500))
            if text and not is_ref:
                sources.append({
                    "source_type": "pymupdf_page",
                    "source_id": f"page_{page_number}",
                    "page": page_number,
                    "text": text,
                })

    table_dir = paper_dir / "pdfplumber_tables"
    log_collect("checkpoint", "pdfplumber_table_dir", source_id=str(table_dir), exists=table_dir.exists())
    if table_dir.exists():
        for table_path in sorted(table_dir.glob("*.csv")):
            log_collect("start", "read_pdfplumber_table", source_id=table_path.name)
            table_text = safe_read_text(table_path)
            log_collect("end", "read_pdfplumber_table", source_id=table_path.name, chars=len(table_text))
            ref_key = f"collect:{paper_id}:pdfplumber_table:{table_path.name}:reference_check"
            if timing_logger:
                timing_logger.start(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_id=table_path.name, chars=min(len(table_text), 1500))
            is_ref = is_reference_like_snippet(table_text[:1500])
            if timing_logger:
                timing_logger.end(ref_key, paper_id=paper_id, stage="collect_sources:reference_check", source_id=table_path.name, chars=min(len(table_text), 1500))
            if table_text and not is_ref:
                page_match = re.search(r"page_(\d+)_", table_path.name)
                page_number = ""
                if page_match:
                    page_number = str(int(page_match.group(1)))
                sources.append({
                    "source_type": "pdfplumber_table",
                    "source_id": table_path.name,
                    "page": page_number,
                    "text": table_text,
                })

    log_collect("checkpoint", "done", source_total=len(sources))
    return sources


def should_skip_evidence_snippet(snippet: str) -> bool:
    return is_reference_like_snippet(snippet)


def detect_edge_termination(source: dict) -> List[dict]:
    text = source.get("normalized_text")
    if text is None:
        text = normalize_for_search(source.get("text", "") or "")
    evidence = []

    for category, term_patterns in EDGE_TERM_PATTERNS.items():
        for term, pattern in term_patterns:
            for match in pattern.finditer(text):
                snippet = context_window(text, match.start(), match.end())

                if should_skip_evidence_snippet(snippet):
                    continue

                ctx = classify_context(snippet)

                score = evidence_score_base(source.get("source_type", ""))
                if category != "generic_edge_termination":
                    score += 5
                else:
                    score += 2

                if source.get("source_type") in {"metadata_title", "grobid_title"}:
                    score += 2
                if has_any(snippet, DEVICE_TERMS):
                    score += 2
                if has_any(snippet, ["termination", "edge", "peripheral", "device structure", "fabricated"]):
                    score += 2
                if ctx["comparison_risk"]:
                    score -= 2

                evidence.append({
                    "field": "edge_termination",
                    "value": category,
                    "raw_value": term,
                    "unit": "",
                    "normalized_value": category,
                    "source_type": source.get("source_type", ""),
                    "source_id": source.get("source_id", ""),
                    "page": source.get("page", ""),
                    "heading": source.get("heading", ""),
                    "snippet": clean_space(snippet),
                    "score": score,
                    **ctx,
                })

    return evidence


def normalize_voltage(value: float, unit: str) -> Tuple[float, str]:
    unit_l = unit.lower().replace(" ", "")
    if unit_l == "kv":
        return value * 1000.0, "V"
    return value, "V"


def normalize_resistance_area(value: float, prefix_unit: str) -> Tuple[float, str]:
    unit_l = normalize_for_search(prefix_unit).lower().replace(" ", "")

    if "mohm" in unit_l or unit_l == "m":
        return value, "mOhm cm^2"

    if "ohm" in unit_l:
        return value * 1000.0, "mOhm cm^2"

    return value, "mOhm cm^2"


def detect_breakdown_voltage(source: dict) -> List[dict]:
    text = source.get("normalized_text")
    if text is None:
        text = normalize_for_search(source.get("text", "") or "")
    evidence = []

    patterns = list(BV_PATTERNS_GENERAL)

    title_like_source = source.get("source_type") in {"metadata_title", "grobid_title"}
    if title_like_source:
        patterns.append(BV_PATTERN_TITLE)

    for pattern in patterns:
        for match in pattern.finditer(text):
            try:
                value = float(match.group("value").replace(",", ""))
                unit_raw = match.group("unit")
            except Exception:
                continue

            normalized_value, normalized_unit = normalize_voltage(value, unit_raw)

            snippet = context_window(text, match.start(), match.end())
            snippet_l = normalize_for_search(snippet).lower()

            if should_skip_evidence_snippet(snippet):
                continue

            if "mv/cm" in snippet_l or "v/cm" in snippet_l or "electric field" in snippet_l:
                continue

            if title_like_source and not has_any(text, DEVICE_TERMS):
                continue

            ctx = classify_context(snippet)

            score = evidence_score_base(source.get("source_type", "")) + 5

            if title_like_source:
                score += 3
            if has_any(snippet, ["measured", "experimental", "fabricated", "leakage", "reverse"]):
                score += 3
            if has_any(snippet, ["designed", "simulated", "calculated", "tcad"]):
                score -= 1
            if ctx["comparison_risk"]:
                score -= 2

            evidence.append({
                "field": "breakdown_voltage",
                "value": value,
                "raw_value": f"{value:g} {unit_raw}",
                "unit": unit_raw,
                "normalized_value": normalized_value,
                "normalized_unit": normalized_unit,
                "source_type": source.get("source_type", ""),
                "source_id": source.get("source_id", ""),
                "page": source.get("page", ""),
                "heading": source.get("heading", ""),
                "snippet": clean_space(snippet),
                "score": score,
                **ctx,
            })

    return evidence


def detect_specific_on_resistance(source: dict) -> List[dict]:
    text = source.get("normalized_text")
    if text is None:
        text = normalize_for_search(source.get("text", "") or "")
    evidence = []

    patterns = list(RON_PATTERNS_GENERAL)

    title_like_source = source.get("source_type") in {"metadata_title", "grobid_title"}
    if title_like_source:
        patterns.append(RON_PATTERN_TITLE)

    for pattern in patterns:
        for match in pattern.finditer(text):
            try:
                value = float(match.group("value").replace(",", ""))
                unit_raw = match.group("unit")
            except Exception:
                continue

            snippet = context_window(text, match.start(), match.end())
            snippet_l = normalize_for_search(snippet).lower()

            if should_skip_evidence_snippet(snippet):
                continue

            ctx = classify_context(snippet)
            unit_norm = normalize_for_search(unit_raw).lower().replace(" ", "")

            if unit_norm == "m":
                has_ron_context = bool(RON_KEYWORD_RE.search(snippet_l))
                if not has_ron_context and not title_like_source:
                    continue

            if title_like_source and not has_any(text, DEVICE_TERMS):
                continue

            normalized_value, normalized_unit = normalize_resistance_area(value, unit_raw)

            score = evidence_score_base(source.get("source_type", "")) + 5

            if title_like_source:
                score += 3
            if source.get("source_type") == "grobid_abstract":
                score += 1
            if RON_KEYWORD_RE.search(snippet_l):
                score += 2
            if has_any(snippet, ["measured", "experimental", "fabricated", "forward", "differential", "demonstrating", "obtained"]):
                score += 3
            if has_any(snippet, ["drift layer", "theoretical", "calculated", "simulated"]):
                score -= 1
            if ctx["comparison_risk"]:
                score -= 2

            evidence.append({
                "field": "specific_on_resistance",
                "value": value,
                "raw_value": f"{value:g} {unit_raw} cm^2",
                "unit": f"{unit_raw} cm^2",
                "normalized_value": normalized_value,
                "normalized_unit": normalized_unit,
                "source_type": source.get("source_type", ""),
                "source_id": source.get("source_id", ""),
                "page": source.get("page", ""),
                "heading": source.get("heading", ""),
                "snippet": clean_space(snippet),
                "score": score,
                **ctx,
            })

    return evidence


def detect_device_type(sources: List[dict], title: str = "") -> List[dict]:
    if title_suppresses_device_type(title):
        return []

    title_sources = [
        source.get("text", "")
        for source in sources
        if source.get("source_type") in {"metadata_title", "grobid_title"}
    ]

    other_sources = [
        source.get("text", "")[:2000]
        for source in sources
        if source.get("source_type") not in {"metadata_title", "grobid_title"}
        and not is_reference_like_snippet(source.get("text", "")[:1500])
    ]

    joined = "\n".join(title_sources + other_sources)
    text = normalize_for_search(joined)

    evidence = []
    for label, pattern in DEVICE_TYPE_PATTERNS:
        match = pattern.search(text)
        if match:
            snippet = context_window(text, match.start(), match.end(), window=250)

            if should_skip_evidence_snippet(snippet):
                continue

            evidence.append({
                "field": "device_type",
                "value": label,
                "raw_value": match.group(0),
                "unit": "",
                "normalized_value": label,
                "source_type": "combined_initial_text",
                "source_id": "combined_initial_text",
                "page": "",
                "heading": "",
                "snippet": clean_space(snippet),
                "score": 1,
                **classify_context(snippet),
            })
            break

    return evidence


def deduplicate_evidence(evidence: List[dict]) -> List[dict]:
    seen = set()
    deduped = []

    for ev in sorted(evidence, key=lambda x: -int(x.get("score", 0))):
        snippet_key = clean_space(ev.get("snippet", ""))[:220].lower()
        key = (
            ev.get("field", ""),
            str(ev.get("normalized_value", "")).lower(),
            ev.get("normalized_unit", ""),
            ev.get("source_type", ""),
            ev.get("source_id", ""),
            snippet_key,
        )

        if key in seen:
            continue

        seen.add(key)
        deduped.append(ev)

    return deduped


def best_evidence(evidence: List[dict], field: str) -> Optional[dict]:
    candidates = [ev for ev in evidence if ev.get("field") == field]
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: -int(x.get("score", 0)))[0]


def top_evidence(evidence: List[dict], field: str, n: int = 3) -> List[dict]:
    candidates = [ev for ev in evidence if ev.get("field") == field]
    return sorted(candidates, key=lambda x: -int(x.get("score", 0)))[:n]


def summarize_edge_types(evidence: List[dict]) -> str:
    vals = []
    for ev in top_evidence(evidence, "edge_termination", n=6):
        value = ev.get("normalized_value") or ev.get("value")
        raw = ev.get("raw_value") or ""
        label = f"{value}: {raw}"
        if label not in vals:
            vals.append(label)
    return " | ".join(vals)


def summarize_values(evidence: List[dict], field: str, n: int = 3) -> str:
    vals = []
    for ev in top_evidence(evidence, field, n=n):
        raw = ev.get("raw_value", "")
        norm = ev.get("normalized_value", "")
        unit = ev.get("normalized_unit", ev.get("unit", ""))
        page = ev.get("page", "")
        source = ev.get("source_id", "")
        page_text = f"p. {page}" if page else source

        try:
            norm_text = f"{float(norm):g}"
        except Exception:
            norm_text = str(norm)

        vals.append(f"{raw} -> {norm_text} {unit} [{page_text}]")
    return " | ".join(vals)


def summarize_snippets(evidence: List[dict], field: str, n: int = 2) -> str:
    snippets = []
    for ev in top_evidence(evidence, field, n=n):
        source = ev.get("source_id", "")
        page = ev.get("page", "")
        loc = f"p. {page}" if page else source
        snippet = ev.get("snippet", "")
        snippets.append(f"[{loc}] {snippet}")
    return " || ".join(snippets)


def classify_paper(evidence: List[dict]) -> Tuple[str, str]:
    best_device = best_evidence(evidence, "device_type")
    has_diode = bool(best_device and int(best_device.get("score", 0)) >= 1)

    if not has_diode:
        return "exclude_candidate", "no diode evidence found"

    best_edge = best_evidence(evidence, "edge_termination")
    best_bv = best_evidence(evidence, "breakdown_voltage")
    best_ron = best_evidence(evidence, "specific_on_resistance")

    edge_score = int(best_edge.get("score", 0)) if best_edge else 0
    bv_score = int(best_bv.get("score", 0)) if best_bv else 0
    ron_score = int(best_ron.get("score", 0)) if best_ron else 0

    has_edge = edge_score >= 7
    has_bv = bv_score >= 7
    has_ron = ron_score >= 7

    if has_edge and has_bv and has_ron:
        risk_flags = []
        for ev in [best_edge, best_bv, best_ron]:
            if ev and ev.get("reference_context"):
                risk_flags.append("reference-section risk")
            elif ev and ev.get("comparison_risk"):
                risk_flags.append("comparison-table/literature risk")

            if ev and ev.get("simulation_context") and not ev.get("measurement_context"):
                risk_flags.append("possibly simulated/designed value")

        if risk_flags:
            return "maybe", "; ".join(sorted(set(risk_flags)))

        return "include_candidate", "diode evidence plus edge termination, breakdown voltage, and specific on-resistance evidence found"

    missing = []
    if not has_edge:
        missing.append("edge termination")
    if not has_bv:
        missing.append("breakdown voltage")
    if not has_ron:
        missing.append("specific on-resistance")

    if len(missing) == 1:
        return "maybe", f"diode evidence found; missing or weak evidence for: {', '.join(missing)}"

    if len(missing) == 2:
        return "low_priority_maybe", f"diode evidence found; missing or weak evidence for: {', '.join(missing)}"

    return "exclude_candidate", "diode evidence found, but no strong evidence for required fields"


def process_paper_dir(
    paper_dir: Path,
    timing_logger: Optional[TimingLogger] = None,
    paper_index: int = 0,
    paper_total: int = 0,
) -> dict:
    metadata = parse_metadata(paper_dir)
    paper_id = metadata.get("paper_id", paper_dir.name)
    title = metadata.get("title", "")
    paper_key = f"paper:{paper_id}"

    if timing_logger:
        timing_logger.start(
            paper_key,
            paper_id=paper_id,
            stage="paper",
            paper_index=paper_index,
            paper_total=paper_total,
            title=title,
        )
        timing_logger.start(f"{paper_key}:collect_sources", paper_id=paper_id, stage="collect_sources")

    sources = collect_sources(paper_dir, timing_logger=timing_logger, paper_id=paper_id)

    if timing_logger:
        timing_logger.end(
            f"{paper_key}:collect_sources",
            paper_id=paper_id,
            stage="collect_sources",
            source_total=len(sources),
        )

    total_raw_chars = sum(len(source.get("text", "") or "") for source in sources)
    if timing_logger:
        timing_logger.log(
            "source_summary",
            paper_id=paper_id,
            stage="source_summary",
            source_total=len(sources),
            chars=total_raw_chars,
        )

    # Normalize each source once per paper. The field detectors all use the same
    # normalized text, and context_window() slices it directly instead of
    # re-normalizing the full source for every evidence match.
    for source_index, source in enumerate(sources, start=1):
        source_type = source.get("source_type", "")
        source_id = source.get("source_id", "")
        raw_text = source.get("text", "") or ""
        normalize_key = f"{paper_key}:normalize:{source_index}"

        if timing_logger:
            timing_logger.start(
                normalize_key,
                paper_id=paper_id,
                stage="normalize_source",
                source_index=source_index,
                source_total=len(sources),
                source_type=source_type,
                source_id=source_id,
                chars=len(raw_text),
            )

        source["normalized_text"] = normalize_for_search(raw_text)

        if timing_logger:
            timing_logger.end(
                normalize_key,
                paper_id=paper_id,
                stage="normalize_source",
                source_index=source_index,
                source_total=len(sources),
                source_type=source_type,
                source_id=source_id,
                chars=len(source.get("normalized_text", "") or ""),
            )

    evidence = []

    if timing_logger:
        timing_logger.start(f"{paper_key}:device_type", paper_id=paper_id, stage="detect_device_type")
    device_evidence = detect_device_type(sources, title=title)
    evidence.extend(device_evidence)
    if timing_logger:
        timing_logger.end(
            f"{paper_key}:device_type",
            paper_id=paper_id,
            stage="detect_device_type",
            matches=len(device_evidence),
        )

    for source_index, source in enumerate(sources, start=1):
        source_type = source.get("source_type", "")
        source_id = source.get("source_id", "")
        normalized_chars = len(source.get("normalized_text", "") or "")

        if timing_logger:
            timing_logger.log(
                "source_start",
                paper_id=paper_id,
                stage="source",
                source_index=source_index,
                source_total=len(sources),
                source_type=source_type,
                source_id=source_id,
                chars=normalized_chars,
            )

        detector_specs = [
            ("detect_edge_termination", detect_edge_termination),
            ("detect_breakdown_voltage", detect_breakdown_voltage),
            ("detect_specific_on_resistance", detect_specific_on_resistance),
        ]

        for detector_name, detector in detector_specs:
            detector_key = f"{paper_key}:{detector_name}:{source_index}"
            if timing_logger:
                timing_logger.start(
                    detector_key,
                    paper_id=paper_id,
                    stage=detector_name,
                    source_index=source_index,
                    source_total=len(sources),
                    source_type=source_type,
                    source_id=source_id,
                    chars=normalized_chars,
                )

            detector_evidence = detector(source)
            evidence.extend(detector_evidence)

            if timing_logger:
                timing_logger.end(
                    detector_key,
                    paper_id=paper_id,
                    stage=detector_name,
                    source_index=source_index,
                    source_total=len(sources),
                    source_type=source_type,
                    source_id=source_id,
                    chars=normalized_chars,
                    matches=len(detector_evidence),
                )

        if timing_logger:
            timing_logger.log(
                "source_end",
                paper_id=paper_id,
                stage="source",
                source_index=source_index,
                source_total=len(sources),
                source_type=source_type,
                source_id=source_id,
                chars=normalized_chars,
            )

    if timing_logger:
        timing_logger.start(f"{paper_key}:deduplicate", paper_id=paper_id, stage="deduplicate_evidence")
    evidence = deduplicate_evidence(evidence)
    if timing_logger:
        timing_logger.end(
            f"{paper_key}:deduplicate",
            paper_id=paper_id,
            stage="deduplicate_evidence",
            matches=len(evidence),
        )

    if timing_logger:
        timing_logger.start(f"{paper_key}:classify", paper_id=paper_id, stage="classify_paper")
    decision, decision_reason = classify_paper(evidence)
    if timing_logger:
        timing_logger.end(
            f"{paper_key}:classify",
            paper_id=paper_id,
            stage="classify_paper",
            decision=decision,
        )

    result = {
        "metadata": metadata,
        "decision": decision,
        "decision_reason": decision_reason,
        "evidence_counts": {
            "device_type": len([ev for ev in evidence if ev.get("field") == "device_type"]),
            "edge_termination": len([ev for ev in evidence if ev.get("field") == "edge_termination"]),
            "breakdown_voltage": len([ev for ev in evidence if ev.get("field") == "breakdown_voltage"]),
            "specific_on_resistance": len([ev for ev in evidence if ev.get("field") == "specific_on_resistance"]),
        },
        "best_evidence": {
            "device_type": best_evidence(evidence, "device_type"),
            "edge_termination": best_evidence(evidence, "edge_termination"),
            "breakdown_voltage": best_evidence(evidence, "breakdown_voltage"),
            "specific_on_resistance": best_evidence(evidence, "specific_on_resistance"),
        },
        "evidence": evidence,
    }

    if timing_logger:
        timing_logger.end(
            paper_key,
            paper_id=paper_id,
            stage="paper",
            paper_index=paper_index,
            paper_total=paper_total,
            decision=decision,
            matches=len(evidence),
        )

    return result


def find_paper_dirs(input_dir: Path) -> List[Path]:
    paper_dirs = []

    for path in sorted(input_dir.iterdir()):
        if not path.is_dir():
            continue

        if (path / "metadata.json").exists() or (path / "fulltext.txt").exists() or (path / "pages.json").exists():
            paper_dirs.append(path)

    return paper_dirs


def write_jsonl(results: List[dict], path: Path, append: bool = False) -> None:
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as f:
        for result in results:
            f.write(json.dumps(result, ensure_ascii=False))
            f.write("\n")


def deduplicate_results_by_identity(results: List[dict]) -> List[dict]:
    """Return one record per paper identity, preserving the earliest record.

    This is kept for backwards-compatible uses where the older record should
    win. For recheck/update workflows, use
    deduplicate_results_by_identity_prefer_latest().
    """
    seen = set()
    deduped = []

    for result in results:
        keys = result_identity_keys(result)
        if keys and (keys & seen):
            continue
        seen.update(keys)
        deduped.append(result)

    return deduped


def deduplicate_results_by_identity_prefer_latest(results: List[dict]) -> List[dict]:
    """Return one record per paper identity, preferring later records.

    This is important for --recheck-existing: rechecked papers should replace
    older evidence records rather than being appended as duplicate rows. The
    function walks from the end, keeps the first occurrence of each identity in
    that reversed order, then reverses again to preserve a stable output order.
    Records without identity keys are retained.
    """
    seen = set()
    deduped_reversed = []

    for result in reversed(results):
        keys = result_identity_keys(result)
        if keys and (keys & seen):
            continue
        seen.update(keys)
        deduped_reversed.append(result)

    return list(reversed(deduped_reversed))


def backup_file(path: Path, reason: str) -> Optional[Path]:
    if not path.exists():
        return None

    safe_reason = re.sub(r"[^a-zA-Z0-9_-]+", "_", reason or "backup").strip("_") or "backup"
    backup_path = path.with_name(f"{path.stem}.backup_{timestamp_for_filename()}_{safe_reason}{path.suffix}")
    shutil.copy2(path, backup_path)
    return backup_path


def write_review_csv(results: List[dict], path: Path) -> None:
    fieldnames = [
        "decision",
        "decision_reason",
        "paper_id",
        "title",
        "doi",
        "zotero_key",
        "attachment_key",
        "source_pdf_path",
        "output_dir",
        "device_type_guess",
        "edge_termination_guess",
        "breakdown_voltage_guess",
        "specific_on_resistance_guess",
        "n_device_type_evidence",
        "n_edge_evidence",
        "n_breakdown_evidence",
        "n_ronsp_evidence",
        "edge_snippets",
        "breakdown_snippets",
        "ronsp_snippets",
    ]

    rows = []

    decision_rank = {
        "include_candidate": 0,
        "maybe": 1,
        "low_priority_maybe": 2,
        "exclude_candidate": 3,
    }

    for result in results:
        metadata = result.get("metadata") or {}
        evidence = result.get("evidence") or []
        counts = result.get("evidence_counts") or {}

        best_device = best_evidence(evidence, "device_type")
        device_guess = best_device.get("normalized_value", "") if best_device else ""

        row = {
            "decision": result.get("decision", ""),
            "decision_reason": result.get("decision_reason", ""),
            "paper_id": metadata.get("paper_id", ""),
            "title": metadata.get("title", ""),
            "doi": metadata.get("doi", ""),
            "zotero_key": metadata.get("zotero_key", ""),
            "attachment_key": metadata.get("attachment_key", ""),
            "source_pdf_path": metadata.get("source_pdf_path", ""),
            "output_dir": metadata.get("output_dir", ""),
            "device_type_guess": device_guess,
            "edge_termination_guess": summarize_edge_types(evidence),
            "breakdown_voltage_guess": summarize_values(evidence, "breakdown_voltage"),
            "specific_on_resistance_guess": summarize_values(evidence, "specific_on_resistance"),
            "n_device_type_evidence": counts.get("device_type", 0),
            "n_edge_evidence": counts.get("edge_termination", 0),
            "n_breakdown_evidence": counts.get("breakdown_voltage", 0),
            "n_ronsp_evidence": counts.get("specific_on_resistance", 0),
            "edge_snippets": summarize_snippets(evidence, "edge_termination"),
            "breakdown_snippets": summarize_snippets(evidence, "breakdown_voltage"),
            "ronsp_snippets": summarize_snippets(evidence, "specific_on_resistance"),
        }

        rows.append(row)

    rows.sort(
        key=lambda r: (
            decision_rank.get(r["decision"], 99),
            r["device_type_guess"] == "",
            -int(r["n_edge_evidence"]),
            -int(r["n_breakdown_evidence"]),
            -int(r["n_ronsp_evidence"]),
            r["title"].lower(),
        )
    )

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_summary(results: List[dict]) -> None:
    counts = {}
    for result in results:
        decision = result.get("decision", "unknown")
        counts[decision] = counts.get(decision, 0) + 1

    print("\nDecision summary:")
    for decision, count in sorted(counts.items(), key=lambda kv: kv[0]):
        print(f"  {decision}: {count}")

    print("\nTop include/maybe candidates:")
    shown = 0
    for result in results:
        if result.get("decision") not in {"include_candidate", "maybe"}:
            continue

        metadata = result.get("metadata") or {}
        print(f"  - {result.get('decision')}: {metadata.get('title', '(untitled)')}")
        shown += 1

        if shown >= 10:
            break


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect edge termination, breakdown voltage, and specific on-resistance evidence in extracted SiC diode papers."
    )

    parser.add_argument(
        "--input-dir",
        default=DEFAULT_INPUT_DIR,
        help=f"Input extraction directory. Default: {DEFAULT_INPUT_DIR}",
    )

    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory. Default: {DEFAULT_OUTPUT_DIR}",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional maximum number of new paper directories to process. 0 means no limit.",
    )

    parser.add_argument(
        "--recheck-existing",
        action="store_true",
        help=(
            "Reprocess papers even if they already appear in the existing evidence JSONL. "
            "By default, already-checked papers are skipped."
        ),
    )

    parser.add_argument(
        "--fresh-output",
        action="store_true",
        help=(
            "Start a fresh evidence JSONL instead of appending new results to the existing one. "
            "This also disables skip-by-existing-output unless --recheck-existing is used."
        ),
    )

    parser.add_argument(
        "--compact-existing",
        action="store_true",
        help=(
            "Do not process papers. Instead, de-duplicate the existing evidence JSONL "
            "in place, keeping the latest record for each paper identity. A timestamped "
            "backup is written first."
        ),
    )

    parser.add_argument(
        "--timing-log",
        default="",
        help=(
            "Path for a real-time JSONL timing log. Default: "
            "<output-dir>/timing_YYYYMMDD_HHMMSS.jsonl"
        ),
    )

    parser.add_argument(
        "--no-timing-log",
        action="store_true",
        help="Disable timing-log file output.",
    )

    parser.add_argument(
        "--show-timing",
        action="store_true",
        help=(
            "Print detailed real-time timing events to the console. By default, "
            "timing events are written only to the JSONL timing log. Regular "
            "progress messages are still printed."
        ),
    )

    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    ensure_dir(output_dir)

    if not input_dir.exists():
        raise SystemExit(f"Input directory not found: {input_dir}")

    evidence_jsonl_path = output_dir / "evidence.jsonl"
    review_csv_path = output_dir / "review_queue.csv"
    timing_log_path = Path(args.timing_log) if args.timing_log else output_dir / f"timing_{timestamp_for_filename()}.jsonl"
    timing_logger = TimingLogger(
        timing_log_path,
        enabled=not args.no_timing_log,
        live_enabled=bool(args.show_timing),
    )

    timing_logger.log(
        "run_start",
        stage="run",
        input_dir=str(input_dir),
        output_dir=str(output_dir),
        evidence_jsonl=str(evidence_jsonl_path),
        review_csv=str(review_csv_path),
        timing_log=str(timing_log_path),
        live=True,
    )
    if args.no_timing_log:
        print("Timing log file output disabled by --no-timing-log.", flush=True)
    else:
        print(f"Timing log JSONL: {timing_log_path}", flush=True)
    if args.show_timing:
        print("Live timing output enabled by --show-timing.", flush=True)
    else:
        print("Live timing output hidden. Use --show-timing to print detailed timing events.", flush=True)

    existing_results = [] if args.fresh_output else load_existing_results(evidence_jsonl_path)
    existing_keys = build_existing_identity_index(existing_results)

    if args.compact_existing:
        compacted_results = deduplicate_results_by_identity_prefer_latest(existing_results)
        backup_path = backup_file(evidence_jsonl_path, "before_compact")
        write_jsonl(compacted_results, evidence_jsonl_path, append=False)
        write_review_csv(compacted_results, review_csv_path)
        timing_logger.log(
            "run_end",
            stage="run",
            mode="compact_existing",
            input_records=len(existing_results),
            compacted_records=len(compacted_results),
            removed_records=len(existing_results) - len(compacted_results),
            backup=str(backup_path or ""),
        )
        timing_logger.close()
        print("\nDone.")
        print("Compacted existing evidence JSONL only; no papers were processed.")
        print(f"Input records: {len(existing_results)}")
        print(f"Compacted records: {len(compacted_results)}")
        print(f"Removed duplicate records: {len(existing_results) - len(compacted_results)}")
        if backup_path:
            print(f"Backup JSONL: {backup_path}")
        print(f"Evidence JSONL: {evidence_jsonl_path}")
        print(f"Review CSV: {review_csv_path}")
        if not args.no_timing_log:
            print(f"Timing log JSONL: {timing_log_path}")
        print_summary(compacted_results)
        return

    paper_dirs = find_paper_dirs(input_dir)

    skipped_existing = 0
    candidate_paper_dirs = []

    for paper_dir in paper_dirs:
        metadata = parse_metadata(paper_dir)
        if not args.recheck_existing and not args.fresh_output and is_already_processed(metadata, existing_keys):
            skipped_existing += 1
            continue
        candidate_paper_dirs.append(paper_dir)

    if args.limit and args.limit > 0:
        candidate_paper_dirs = candidate_paper_dirs[:args.limit]

    print(f"Found {len(paper_dirs)} paper directories.")
    if not args.recheck_existing and not args.fresh_output:
        print(f"Skipping {skipped_existing} paper directories already present in {evidence_jsonl_path}.")
    print(f"Processing {len(candidate_paper_dirs)} paper directories.")

    new_results = []

    for index, paper_dir in enumerate(candidate_paper_dirs, start=1):
        print(f"[{index}/{len(candidate_paper_dirs)}] {paper_dir.name}")

        try:
            result = process_paper_dir(
                paper_dir,
                timing_logger=timing_logger,
                paper_index=index,
                paper_total=len(candidate_paper_dirs),
            )
        except Exception as exc:
            timing_logger.log(
                "paper_error",
                paper_id=paper_dir.name,
                stage="paper",
                paper_index=index,
                paper_total=len(candidate_paper_dirs),
                error=str(exc),
            )
            result = {
                "metadata": {
                    "paper_id": paper_dir.name,
                    "output_dir": str(paper_dir),
                },
                "decision": "error",
                "decision_reason": str(exc),
                "evidence_counts": {},
                "best_evidence": {},
                "evidence": [],
            }

        new_results.append(result)

    evidence_jsonl_backup_path = None

    if args.fresh_output:
        evidence_jsonl_backup_path = backup_file(evidence_jsonl_path, "before_fresh_output")
        combined_results = deduplicate_results_by_identity_prefer_latest(new_results)
        write_jsonl(combined_results, evidence_jsonl_path, append=False)
    elif args.recheck_existing:
        # Rechecked records are replacements, not additive history. Keep the
        # latest record for each paper identity and rewrite evidence.jsonl after
        # making a timestamped backup. This prevents --recheck-existing from
        # tripling the JSONL and downstream evidence table.
        evidence_jsonl_backup_path = backup_file(evidence_jsonl_path, "before_recheck_replace")
        combined_results = deduplicate_results_by_identity_prefer_latest(existing_results + new_results)
        write_jsonl(combined_results, evidence_jsonl_path, append=False)
    else:
        # Normal append-safe mode: append only newly processed papers. The
        # review queue is built from a de-duplicated in-memory view so accidental
        # old duplicates do not multiply there, but the JSONL is not rewritten
        # unless --recheck-existing, --fresh-output, or --compact-existing is used.
        write_jsonl(new_results, evidence_jsonl_path, append=True)
        combined_results = deduplicate_results_by_identity_prefer_latest(existing_results + new_results)

    write_review_csv(combined_results, review_csv_path)

    timing_logger.log(
        "run_end",
        stage="run",
        newly_processed=len(new_results),
        skipped_existing=skipped_existing,
        combined_results=len(combined_results),
        evidence_jsonl_rewritten=bool(args.fresh_output or args.recheck_existing),
        evidence_jsonl_backup=str(evidence_jsonl_backup_path or ""),
    )
    timing_logger.close()

    print("\nDone.")
    print(f"Newly processed papers: {len(new_results)}")
    if not args.recheck_existing and not args.fresh_output:
        print(f"Skipped already-checked papers: {skipped_existing}")
    print(f"Evidence JSONL: {evidence_jsonl_path}")
    if evidence_jsonl_backup_path:
        print(f"Backup JSONL: {evidence_jsonl_backup_path}")
    if args.recheck_existing and not args.fresh_output:
        print("Recheck mode: existing evidence records were replaced/compacted instead of appended as duplicates.")
    print(f"Review CSV: {review_csv_path}")
    if not args.no_timing_log:
        print(f"Timing log JSONL: {timing_log_path}")

    print_summary(combined_results)


if __name__ == "__main__":
    main()