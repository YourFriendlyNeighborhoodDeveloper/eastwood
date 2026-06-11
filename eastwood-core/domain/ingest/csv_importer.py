"""
CSV lead importer.

Accepts any vendor CSV format and maps columns to canonical lead fields using
fuzzy matching.  Unknown columns are stored in raw_payload for audit.
Returns a list of normalized dicts ready to publish to leads.raw.
"""
from __future__ import annotations

import csv
import io
import re
from typing import Any

from domain.normalization import normalize_lead_payload

# Canonical field → list of aliases (lower-case, no spaces/underscores)
_ALIASES: dict[str, list[str]] = {
    "first_name":   ["firstname", "first", "fname", "givenname"],
    "last_name":    ["lastname", "last", "lname", "surname", "familyname"],
    "phone_e164":   ["phone", "phonenumber", "mobile", "cell", "telephone", "mobilephone", "cellphone"],
    "email":        ["email", "emailaddress", "email_address"],
    "state":        ["state", "statecode", "st", "province"],
    "zip_code":     ["zip", "zipcode", "postalcode", "postal"],
    "age":          ["age", "currentage", "ageatinquiry"],
    "product_line": ["product", "productline", "insurancetype", "linetype", "coverage", "plan"],
    "source":       ["source", "leadsource", "vendor", "origin", "channel"],
    "gender":       ["gender", "sex"],
    "tobacco":      ["tobacco", "smoker", "smokeuse", "tobaccouse"],
    "face_value":   ["facevalue", "coverageamount", "faceamount", "deathbenefit", "amount"],
    "batch_id":     ["batchid", "batch", "listid", "fileid"],
}

_PRODUCT_ALIASES: dict[str, str] = {
    "fe":                   "final_expense",
    "finalexpense":         "final_expense",
    "final_expense":        "final_expense",
    "medicare":             "medicare_supplement",
    "medicaresupplement":   "medicare_supplement",
    "medicare_supplement":  "medicare_supplement",
    "medicareadvantage":    "medicare_advantage",
    "medicare_advantage":   "medicare_advantage",
    "mapd":                 "medicare_advantage",
    "ma":                   "medicare_advantage",
    "aca":                  "aca",
    "obamacare":            "aca",
    "life":                 "life",
    "term":                 "life",
    "annuity":              "annuity",
}


def _normalise_key(raw: str) -> str:
    return re.sub(r"[\s_\-]+", "", raw.lower())


def _build_column_map(headers: list[str]) -> dict[str, str]:
    """Map each CSV header to its canonical field name (or None if unknown)."""
    mapping: dict[str, str] = {}
    for h in headers:
        key = _normalise_key(h)
        matched = False
        for canonical, aliases in _ALIASES.items():
            if key in aliases or key == canonical.replace("_", ""):
                mapping[h] = canonical
                matched = True
                break
        if not matched:
            mapping[h] = h  # keep raw for audit
    return mapping


def _coerce_product(raw: str | None) -> str | None:
    if not raw:
        return None
    return _PRODUCT_ALIASES.get(_normalise_key(raw))


def import_csv(
    content: bytes | str,
    source_tag: str = "csv_import",
    batch_id: str | None = None,
    tenant_id: str | None = None,
    assume_consent: bool = False,
    consent_source_url: str = "",
) -> list[dict[str, Any]]:
    """Parse CSV bytes/str and return a list of normalised lead dicts.

    Each dict is ready to publish directly to leads.raw.
    Rows that produce no valid phone are skipped and counted in the
    returned list under a sentinel key ``_skipped`` (not a real lead).
    """
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig", errors="replace")

    reader = csv.DictReader(io.StringIO(content))
    if not reader.fieldnames:
        return []

    col_map = _build_column_map(list(reader.fieldnames))

    results: list[dict[str, Any]] = []
    skipped = 0

    for row in reader:
        raw: dict[str, Any] = {}
        extra: dict[str, Any] = {}

        for header, value in row.items():
            value = (value or "").strip()
            canonical = col_map.get(header, header)
            if canonical in _ALIASES:
                raw[canonical] = value
            else:
                extra[canonical] = value

        # Normalise known fields
        payload: dict[str, Any] = {
            "first_name":   raw.get("first_name") or None,
            "last_name":    raw.get("last_name") or None,
            "email":        raw.get("email") or None,
            "state":        (raw.get("state") or "").upper()[:2] or None,
            "zip_code":     raw.get("zip_code") or None,
            "product_line": _coerce_product(raw.get("product_line")),
            "source":       raw.get("source") or source_tag,
            "source_batch_id": raw.get("batch_id") or batch_id,
            "tenant_id":    tenant_id,
            "raw_payload":  {**raw, **extra},
        }

        # Age — must be integer
        try:
            payload["age"] = int(float(raw["age"])) if raw.get("age") else None
        except (ValueError, TypeError):
            payload["age"] = None

        # Phone — normalisation handled by normalize_lead_payload
        payload["phone_e164"] = raw.get("phone_e164") or ""

        if assume_consent:
            payload["express_written_consent"] = True
            payload["consent_source"] = source_tag
            payload["consent_source_url"] = consent_source_url

        normalized = normalize_lead_payload(payload)
        if not normalized.get("phone_e164"):
            skipped += 1
            continue

        results.append(normalized)

    if skipped:
        results.append({"_skipped": skipped, "_meta": True})

    return results
