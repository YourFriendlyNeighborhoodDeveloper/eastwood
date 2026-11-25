"""Outbound text policy — every message Imogen sends passes these checks.

Ported from the voice policy engine (ai-agent/src/policy) and extended for
text: a dollar amount may appear only if it is a coverage amount or premium of
a quote STORED for this conversation; no approval guarantees; no "Medicare is
a government benefit"; never ask for an SSN or bank details; no vague premium
ranges; the first text identifies the agency, says it is an AI assistant and
says how to opt out; at most three segments.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .segments import encoding, segments

MAX_SEGMENTS = 3
_AMOUNT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?")
_GUARANTEE = re.compile(r"\b(guaranteed\s+(approval|acceptance)|cannot\s+be\s+(denied|declined)|can'?t\s+be\s+(denied|declined)|always\s+approved|no\s+questions?\s+asked)\b", re.I)
_GOVERNMENT = re.compile(r"\b(government\s+(program|benefit)|medicare\s+is\s+free|free\s+medicare|federally\s+funded\s+benefit)\b", re.I)
_SENSITIVE = re.compile(r"\b(ssn|social\s+security\s+number|bank\s+account|routing\s+number|card\s+number)\b", re.I)
_RANGE = re.compile(r"\$\s?\d+(?:\.\d{2})?\s*(?:-|to|and)\s*\$?\s?\d+(?:\.\d{2})?", re.I)


def amounts_in(text: str) -> list[Decimal]:
    out = []
    for whole, cents in _AMOUNT.findall(text):
        try:
            out.append(Decimal(whole.replace(",", "") + ("." + cents if cents else "")))
        except InvalidOperation:
            continue
    return out


def validate(text: str, *, allowed_amounts: set[Decimal], first_message: bool, agency: str) -> list[str]:
    problems: list[str] = []
    for amount in amounts_in(text):
        if amount not in allowed_amounts:
            problems.append(f"amount_not_from_stored_quote:{amount}")
    if _GUARANTEE.search(text):
        problems.append("misleading_guarantee")
    if _GOVERNMENT.search(text):
        problems.append("medicare_government_benefit")
    if _SENSITIVE.search(text):
        problems.append("requests_sensitive_data")
    if _RANGE.search(text):
        problems.append("vague_premium_range")
    if first_message:
        low = text.lower()
        if agency.lower() not in low:
            problems.append("first_text_missing_agency")
        if " ai " not in f" {low} " and "ai assistant" not in low:
            problems.append("first_text_missing_ai_disclosure")
        if "stop" not in low:
            problems.append("first_text_missing_opt_out")
    if encoding(text) != "GSM-7":
        problems.append("non_gsm_characters")
    if segments(text) > MAX_SEGMENTS:
        problems.append("too_long")
    return problems
