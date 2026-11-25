"""SMS encoding and segment math — what each text actually costs.

A text in the GSM-7 alphabet fits 160 characters in one segment (153 per
segment when split); a single character outside it (curly quote, em dash,
emoji) switches the whole message to UCS-2: 70 / 67 characters per segment,
roughly doubling the cost. Outbound text is normalized to GSM-7 first.
"""
from __future__ import annotations

import math

_GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXT = set("^{}\\[~]|€\f")
_REPLACE = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", " ": " ", " ": " ", " ": " ", "•": "-", "·": "-",
}


def to_gsm(text: str) -> str:
    return "".join(_REPLACE.get(ch, ch) for ch in text)


def encoding(text: str) -> str:
    return "GSM-7" if all(ch in _GSM_BASIC or ch in _GSM_EXT for ch in text) else "UCS-2"


def segments(text: str) -> int:
    if encoding(text) == "GSM-7":
        units = sum(2 if ch in _GSM_EXT else 1 for ch in text)
        return 1 if units <= 160 else math.ceil(units / 153)
    units = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)
    return 1 if units <= 70 else math.ceil(units / 67)
