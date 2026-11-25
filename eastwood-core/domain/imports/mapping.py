"""Column mapping and per-row normalization for bulk lead imports.

Vendor and CRM exports name the same field a dozen ways ("Other Phone 1",
"Cell", "Date Created", "TCPA Consent"...). Headers are matched against alias
lists after normalization (lowercase, alphanumerics only); an operator can
override any header explicitly. Several columns may map to "phone": the first
valid number in column order wins.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from domain.normalization import normalize_phone, normalize_product

from .states import normalize_state

# Canonical field → header aliases (normalized: lowercase alphanumerics).
ALIASES: dict[str, tuple[str, ...]] = {
    "consent_certificate": ("trustedform", "trustedformcerturl", "trustedformcert", "trustedformurl",
                            "certurl", "jornayaleadid", "leadidtoken", "leadid_token", "universalleadid"),
    "external_id": ("leadid", "id", "recordid", "externalid", "contactid", "customerid", "crmid", "leadnumber"),
    "first_name": ("firstname", "first", "fname", "givenname"),
    "last_name": ("lastname", "last", "lname", "surname", "familyname"),
    "full_name": ("name", "fullname", "contactname", "leadname", "customername"),
    "phone": ("phone", "phonenumber", "phone1", "primaryphone", "mobile", "mobilephone", "cell", "cellphone",
              "cellnumber", "wireless", "telephone", "homephone", "otherphone", "otherphone1", "otherphone2",
              "phone2", "workphone", "bestphone", "contactnumber", "dayphone", "eveningphone"),
    "email": ("email", "emailaddress", "primaryemail"),
    "state": ("state", "statecode", "st", "province", "region", "mailingstate"),
    "zip_code": ("zip", "zipcode", "postalcode", "postal", "zip5", "mailingzip"),
    "city": ("city", "town", "mailingcity"),
    "dob": ("dob", "dateofbirth", "birthdate", "birthday"),
    "sex": ("sex", "gender"),
    "age": ("age", "currentage", "ageatinquiry"),
    "product_line": ("product", "productline", "insurancetype", "linetype", "coverage", "coveragetype",
                     "plan", "interest", "productinterest", "leadtype"),
    "source": ("source", "leadsource", "vendor", "origin", "channel", "subsource"),
    "lead_owner": ("owner", "leadowner", "agent", "assignedto", "assignedagent", "producer", "agentname",
                   "salesrep", "rep"),
    "acquired_at": ("datecreated", "created", "createddate", "createdon", "leaddate", "date", "dateadded",
                    "received", "receiveddate", "entrydate", "leadcreated", "inquirydate", "submitted",
                    "submitteddate"),
    "consent_text": ("tcpaconsent", "consenttext", "consentlanguage", "tcpalanguage", "optintext",
                     "disclosure", "tcpadisclosure", "consent"),
    "consent_captured_at": ("consentdate", "consenttimestamp", "optindate", "optintimestamp", "tcpadate",
                            "tcpatimestamp", "consentedat", "optedinat"),
    "consent_ip": ("ip", "ipaddress", "consentip", "optinip"),
    "consent_url": ("consenturl", "landingpage", "pageurl", "sourceurl", "webpage", "formurl", "optinurl"),
    "dnc_flag": ("dnc", "donotcall", "optout", "optedout", "unsubscribed", "donotcontact", "stopcalling"),
    "tobacco": ("tobacco", "smoker", "tobaccouse"),
    "face_value": ("facevalue", "coverageamount", "faceamount", "deathbenefit"),
}
_ALIAS_INDEX: dict[str, str] = {a: canon for canon, aliases in ALIASES.items() for a in aliases}
CANONICAL_FIELDS = frozenset(ALIASES)

_TRUTHY = {"1", "y", "yes", "true", "t", "x", "dnc", "opted out", "optout"}


def phone_region_of(e164: str) -> str | None:
    """ISO region of a +1 number: "US", or e.g. "CA" / "PR" — NANP numbers outside
    the US normalize as valid but are not covered by the US National DNC Registry."""
    import phonenumbers

    try:
        return phonenumbers.region_code_for_number(phonenumbers.parse(e164, None))
    except phonenumbers.NumberParseException:
        return None


def normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(header or "").lower())


def build_column_map(headers: list[str], overrides: dict[str, str] | None = None) -> dict[int, str]:
    """Column index → canonical field. Unmapped columns are absent (kept raw)."""
    overrides = {normalize_header(k): v for k, v in (overrides or {}).items()}
    mapping: dict[int, str] = {}
    for idx, header in enumerate(headers):
        key = normalize_header(header)
        if key in overrides:
            target = overrides[key]
            if target in CANONICAL_FIELDS:
                mapping[idx] = target
            continue
        canon = _ALIAS_INDEX.get(key)
        if canon:
            mapping[idx] = canon
    return mapping


def describe_column_map(headers: list[str], mapping: dict[int, str]) -> dict[str, str]:
    return {headers[i]: f for i, f in sorted(mapping.items())}


_DATE_FORMATS = (
    "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S.%f",
    "%m/%d/%Y", "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%y", "%m/%d/%y %H:%M", "%m-%d-%Y", "%d-%b-%Y", "%b %d, %Y", "%B %d, %Y", "%Y/%m/%d",
)


def parse_datetime(raw: Any) -> datetime | None:
    """Best-effort timestamp parse; naive values are taken as UTC."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _age_from_dob(dob: datetime, today: date) -> int | None:
    years = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    return years if 0 < years < 120 else None


@dataclass
class RowIssue:
    severity: str  # "error" rejects the row; "warning" imports it
    code: str
    field: str | None = None
    detail: str | None = None


@dataclass
class NormalizedRow:
    row_number: int
    phone_e164: str | None
    fields: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)
    issues: list[RowIssue] = field(default_factory=list)

    @property
    def rejected(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


def _sex(raw: Any) -> str | None:
    v = str(raw or "").strip().lower()
    return "M" if v in ("m", "male", "man") else "F" if v in ("f", "female", "woman") else None


def _yes_no(raw: Any) -> bool | None:
    v = str(raw or "").strip().lower()
    if v in ("y", "yes", "true", "1", "smoker", "tobacco", "t"):
        return True
    if v in ("n", "no", "false", "0", "non-smoker", "nonsmoker", "non-tobacco", "nt", "ns"):
        return False
    return None


def _cell(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # spreadsheet numbers: 3135550101.0 → "3135550101"
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_row(
    row_number: int,
    headers: list[str],
    values: list[Any],
    mapping: dict[int, str],
    *,
    today: date | None = None,
) -> NormalizedRow:
    today = today or datetime.now(timezone.utc).date()
    mapped: dict[str, list[Any]] = {}
    raw: dict[str, Any] = {}
    for idx, header in enumerate(headers):
        value = _cell(values[idx]) if idx < len(values) else None
        if value in (None, ""):
            continue
        raw[str(header)] = value if isinstance(value, (str, int, float, bool)) else str(value)
        canon = mapping.get(idx)
        if canon:
            mapped.setdefault(canon, []).append(value)

    def first(name: str) -> Any:
        vals = mapped.get(name) or []
        return vals[0] if vals else None

    out = NormalizedRow(row_number=row_number, phone_e164=None, raw=raw)

    phones = mapped.get("phone") or []
    for candidate in phones:
        e164 = normalize_phone(str(candidate))
        if e164:
            out.phone_e164 = e164
            break
    phone_region = phone_region_of(out.phone_e164) if out.phone_e164 else None
    if not phones:
        out.issues.append(RowIssue("error", "missing_phone", "phone"))
    elif not out.phone_e164:
        out.issues.append(RowIssue("error", "invalid_phone", "phone", f"{len(phones)} candidate(s), none valid"))

    first_name, last_name = first("first_name"), first("last_name")
    if not (first_name or last_name) and first("full_name"):
        parts = str(first("full_name")).split()
        first_name, last_name = (parts[0], " ".join(parts[1:]) or None) if parts else (None, None)
    if not (first_name or last_name):
        out.issues.append(RowIssue("warning", "missing_name", "first_name"))

    state_raw = first("state")
    state = normalize_state(state_raw)
    if state_raw and not state:
        out.issues.append(RowIssue("warning", "unknown_state", "state", "unrecognized value"))
    elif not state_raw:
        out.issues.append(RowIssue("warning", "missing_state", "state"))

    age = None
    dob = parse_datetime(first("dob"))
    if dob:
        age = _age_from_dob(dob, today)
    if age is None and first("age") is not None:
        try:
            a = int(float(str(first("age"))))
            age = a if 0 < a < 120 else None
        except ValueError:
            age = None

    acquired_raw = first("acquired_at")
    acquired_at = parse_datetime(acquired_raw)
    if acquired_raw and not acquired_at:
        out.issues.append(RowIssue("warning", "bad_date", "acquired_at", "unparseable lead date"))

    consent_ts_raw = first("consent_captured_at")
    consent_captured_at = parse_datetime(consent_ts_raw)
    if consent_ts_raw and not consent_captured_at:
        out.issues.append(RowIssue("warning", "bad_date", "consent_captured_at", "unparseable consent date"))

    product_raw = first("product_line")
    dnc_raw = first("dnc_flag")
    out.fields = {
        "first_name": (str(first_name)[:128] if first_name else None),
        "last_name": (str(last_name)[:128] if last_name else None),
        "email": (str(first("email"))[:256] if first("email") else None),
        "state": state,
        "zip_code": (str(first("zip_code"))[:10] if first("zip_code") else None),
        "age": age,
        "product_line": normalize_product(str(product_raw)) if product_raw else None,
        "product_raw": str(product_raw) if product_raw else None,
        "source_detail": str(first("source"))[:128] if first("source") else None,
        "lead_owner": str(first("lead_owner"))[:128] if first("lead_owner") else None,
        "external_id": str(first("external_id"))[:128] if first("external_id") else None,
        "acquired_at": acquired_at,
        "consent_text": str(first("consent_text")) if first("consent_text") else None,
        "consent_captured_at": consent_captured_at,
        "consent_ip": str(first("consent_ip"))[:45] if first("consent_ip") else None,
        "consent_url": str(first("consent_url")) if first("consent_url") else None,
        "consent_certificate": str(first("consent_certificate")) if first("consent_certificate") else None,
        "source_dnc": str(dnc_raw).strip().lower() in _TRUTHY if dnc_raw is not None else False,
        "sex": _sex(first("sex")),
        "tobacco": _yes_no(first("tobacco")),
        "phone_region": phone_region,
    }
    return out
