"""
PDF lead importer — structured tables only (vendor export PDFs).

Uses pdfplumber to extract tables page-by-page, then feeds each row through
the same CSV column mapper so the rest of the pipeline is identical.

Requires: pdfplumber (already in requirements if installed; add it if not).
"""
from __future__ import annotations

import io
from typing import Any

from .csv_importer import _build_column_map, _coerce_product, _normalise_key, import_csv


def import_pdf(
    content: bytes,
    source_tag: str = "pdf_import",
    batch_id: str | None = None,
    tenant_id: str | None = None,
    assume_consent: bool = False,
    consent_source_url: str = "",
) -> list[dict[str, Any]]:
    """Extract all tables from a PDF and return normalised lead dicts.

    Tries every table on every page and uses the first one with at least
    3 columns that looks like lead data (has a phone-ish column).
    Converts the winning table to CSV text and delegates to import_csv.
    """
    try:
        import pdfplumber
    except ImportError:
        raise RuntimeError(
            "pdfplumber is required for PDF import. "
            "Add 'pdfplumber' to requirements.txt and rebuild the container."
        )

    import csv as _csv

    rows: list[list[str]] = []
    headers: list[str] | None = None

    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            tables = page.extract_tables()
            for table in tables:
                if not table or len(table) < 2:
                    continue
                # First non-empty row is treated as headers
                candidate_headers = [str(c or "").strip() for c in table[0]]
                if len(candidate_headers) < 3:
                    continue
                # Accept if at least one column looks like a phone field
                normalised = [_normalise_key(h) for h in candidate_headers]
                phone_like = {"phone", "phonenumber", "mobile", "cell", "telephone"}
                if not any(n in phone_like for n in normalised):
                    continue
                # Found a usable table — take it and stop
                headers = candidate_headers
                rows = [[str(c or "").strip() for c in r] for r in table[1:]]
                break
            if headers:
                break

    if not headers or not rows:
        return []

    # Serialise to CSV text so import_csv handles all normalisation
    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    csv_text = buf.getvalue()

    return import_csv(
        content=csv_text,
        source_tag=source_tag,
        batch_id=batch_id,
        tenant_id=tenant_id,
        assume_consent=assume_consent,
        consent_source_url=consent_source_url,
    )
