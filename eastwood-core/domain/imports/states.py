"""US state normalization: full names, USPS codes, and common abbreviations.

The old importer took the first two letters of whatever was in the column,
which turned "NEW YORK" into "NE" (Nebraska) and "TEXAS" into "TE".
"""
from __future__ import annotations

import re

_NAMES: dict[str, str] = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC",
    "WASHINGTON DC": "DC", "WASHINGTON D C": "DC", "FLORIDA": "FL", "GEORGIA": "GA",
    "HAWAII": "HI", "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA",
    "KANSAS": "KS", "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI",
    "WYOMING": "WY",
    # Territories normalize, but the TCPA window table does not cover them.
    "PUERTO RICO": "PR", "GUAM": "GU", "US VIRGIN ISLANDS": "VI", "VIRGIN ISLANDS": "VI",
    "AMERICAN SAMOA": "AS", "NORTHERN MARIANA ISLANDS": "MP",
}
_CODES = frozenset(_NAMES.values())
# Older / informal abbreviations seen in vendor exports.
_INFORMAL = {"MICH": "MI", "WIS": "WI", "WISC": "WI", "CALIF": "CA", "CAL": "CA", "PENN": "PA",
             "PENNA": "PA", "MASS": "MA", "MINN": "MN", "TENN": "TN", "TEX": "TX", "FLA": "FL",
             "ILL": "IL", "IND": "IN", "WASH": "WA", "ARIZ": "AZ", "COLO": "CO", "CONN": "CT",
             "OKLA": "OK", "OREG": "OR", "ORE": "OR", "N CAROLINA": "NC", "S CAROLINA": "SC",
             "N DAKOTA": "ND", "S DAKOTA": "SD", "W VIRGINIA": "WV"}


def normalize_state(raw: object) -> str | None:
    """Return the USPS code, or None when the value is not a recognizable state."""
    if raw is None:
        return None
    text = re.sub(r"[^A-Z ]+", " ", str(raw).upper())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if text in _CODES:
        return text
    if text in _NAMES:
        return _NAMES[text]
    if text in _INFORMAL:
        return _INFORMAL[text]
    return None
