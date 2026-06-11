from __future__ import annotations

import re
from typing import Any

import phonenumbers


_PRODUCT_KEYWORDS: dict[str, list[str]] = {
    "final_expense": ["final expense", "burial", "funeral", "whole life", "fe"],
    "medicare_advantage": ["medicare advantage", "ma plan", "mapd", "part c"],
    "medicare_supplement": ["medigap", "medicare supplement", "med supp"],
    "aca": ["aca", "marketplace", "obamacare", "health insurance", "affordable care"],
    "annuity": ["annuity", "retirement income", "fixed annuity"],
    "life": ["life insurance", "term life", "term", "life"],
}

_CONSENT_PATTERNS = [
    re.compile(r"express\s+written\s+consent", re.IGNORECASE),
    re.compile(r"i\s+agree\s+to\s+be\s+contacted", re.IGNORECASE),
    re.compile(r"consent\s+to\s+receive\s+(calls?|texts?|sms)", re.IGNORECASE),
    re.compile(r"tcpa\s+consent", re.IGNORECASE),
    re.compile(r"electronic\s+signature", re.IGNORECASE),
]


def normalize_phone(raw: str, default_region: str = "US") -> str | None:
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, default_region)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return None


def normalize_product(raw: str | None) -> str | None:
    if not raw:
        return None
    lower = raw.lower().strip()
    # Exact match against known product keys (handles "final_expense" style input)
    if lower in _PRODUCT_KEYWORDS:
        return lower
    # Keyword search against a space-normalized version
    spaced = lower.replace("_", " ")
    for product, keywords in _PRODUCT_KEYWORDS.items():
        for kw in keywords:
            if kw in spaced:
                return product
    return None


def normalize_consent(text: str | None) -> bool:
    if not text:
        return False
    return any(p.search(text) for p in _CONSENT_PATTERNS)


def coerce_age(raw: Any) -> int | None:
    if raw is None:
        return None
    try:
        age = int(raw)
        if 0 < age < 130:
            return age
    except (ValueError, TypeError):
        pass
    return None


def normalize_lead_payload(payload: dict) -> dict:
    normalized = dict(payload)

    if phone := payload.get("phone") or payload.get("phone_number"):
        normalized["phone_e164"] = normalize_phone(str(phone))

    if product := payload.get("product") or payload.get("product_line") or payload.get("interest"):
        normalized["product_line"] = normalize_product(str(product))

    if age := payload.get("age"):
        normalized["age"] = coerce_age(age)

    # Honour an explicit boolean flag first; fall back to text-pattern matching.
    consent_text = payload.get("consent_text") or payload.get("tcpa_consent") or ""
    normalized["express_written_consent"] = (
        bool(payload.get("express_written_consent"))
        or normalize_consent(str(consent_text))
    )

    return normalized
