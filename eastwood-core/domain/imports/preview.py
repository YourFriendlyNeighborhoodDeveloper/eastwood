"""Preview an uploaded lead file before importing it.

Shows how each column will be read (automatic mapping plus suggestions for
unmapped columns), the first rows exactly as the importer will normalize them,
and the rows that need a human: invalid or missing phones, unknown states,
low-confidence OCR. The operator can override any mapping and preview again.
Nothing is written to the database.
"""
from __future__ import annotations

import difflib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from config.settings import get_settings

from .mapping import ALIASES, CANONICAL_FIELDS, build_column_map, normalize_header, normalize_row
from .readers import OCR_CONFIDENCE_COLUMN, detect_format, open_rows, pdf_extraction_rows

# Scanned PDFs: the preview OCRs only the first pages (about 2 s each);
# the import itself reads every page.
PREVIEW_OCR_PAGES = 3

_ALL_ALIASES = {alias: field for field, aliases in ALIASES.items() for alias in aliases}


def _suggest(header: str) -> list[str]:
    key = normalize_header(header)
    close = difflib.get_close_matches(key, list(_ALL_ALIASES), n=3, cutoff=0.72)
    return list(dict.fromkeys(_ALL_ALIASES[c] for c in close))


def preview_file(path: Path, overrides: dict | None = None, *, sample: int = 25, scan: int = 1000) -> dict:
    s = get_settings()
    fmt = detect_format(path)
    pdf = None
    if fmt == "pdf":
        from .pdf_reader import extract_pdf

        ex = extract_pdf(path, ocr_max_pages=PREVIEW_OCR_PAGES)
        headers, rows = pdf_extraction_rows(ex)
        pdf = {"method": ex.method, "pages": ex.pages, "pages_read": ex.pages_read}
    else:
        fmt, headers, rows = open_rows(path, fmt)
    mapping = build_column_map(headers, overrides)
    today = datetime.now(timezone.utc).date()
    issue_counts: Counter = Counter()
    phones: Counter = Counter()
    sample_rows, attention = [], []
    scanned = 0
    for n, values in rows:
        scanned += 1
        r = normalize_row(n, headers, values, mapping, today=today)
        for i in r.issues:
            issue_counts[i.code] += 1
        conf = r.raw.get(OCR_CONFIDENCE_COLUMN)
        low_ocr = conf is not None and float(conf) < s.ocr_min_confidence
        if low_ocr:
            issue_counts["low_ocr_confidence"] += 1
        if r.phone_e164:
            phones[r.phone_e164] += 1
        row = {"row": n, "phone_e164": r.phone_e164, "first_name": r.fields.get("first_name"),
               "last_name": r.fields.get("last_name"), "state": r.fields.get("state"), "age": r.fields.get("age"),
               "product_line": r.fields.get("product_line"), "has_consent_text": bool(r.fields.get("consent_text")),
               "ocr_confidence": float(conf) if conf is not None else None,
               "issues": [i.code for i in r.issues] + (["low_ocr_confidence"] if low_ocr else [])}
        if len(sample_rows) < sample:
            sample_rows.append(row)
        if (r.rejected or low_ocr) and len(attention) < 50:
            attention.append({**row, "raw": {k: v for k, v in r.raw.items() if k != OCR_CONFIDENCE_COLUMN}})
        if scanned >= scan:
            break
    mapped = {h: mapping.get(i) for i, h in enumerate(headers) if h != OCR_CONFIDENCE_COLUMN}
    unmapped = [h for h, f in mapped.items() if f is None]
    return {
        "format": fmt,
        "headers": [h for h in headers if h != OCR_CONFIDENCE_COLUMN],
        "mapping": mapped,
        "fields": sorted(CANONICAL_FIELDS),
        "suggestions": {h: _suggest(h) for h in unmapped if _suggest(h)},
        "phone_column_found": "phone" in mapping.values(),
        "rows_scanned": scanned,
        "valid_phones": sum(phones.values()),
        "duplicate_phones_in_sample": sum(c - 1 for c in phones.values() if c > 1),
        "issue_counts": dict(issue_counts),
        "ocr": any(OCR_CONFIDENCE_COLUMN == h for h in headers),
        "pdf": pdf,
        "sample": sample_rows,
        "needs_attention": attention,
    }
