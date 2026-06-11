import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..core.clock import now_utc
from .phone import to_e164
from .product import normalize_product
from .consent_capture import extract_consent


def _parse_created_at(payload: Dict[str, Any]) -> datetime:
    candidates = [
        payload.get("created_at"),
        payload.get("ts"),
        payload.get("timestamp"),
        payload.get("lead_ts"),
        payload.get("opt_in_ts"),
        payload.get("consent_ts"),
    ]
    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if s == "":
            continue
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).replace(microsecond=0)
        except Exception:
            continue
    return now_utc()


def _lead_id(source: str, product: str, phone_e164: str, created_at: datetime, payload: Dict[str, Any]) -> str:
    vendor_id = payload.get("vendor_lead_id") or payload.get("lead_id") or payload.get("id") or ""
    base = f"{source}|{vendor_id}|{product}|{phone_e164}|{created_at.isoformat()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def normalize(source: str, payload: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    product = normalize_product(payload)
    if not product:
        return None, "unknown_product"

    raw_phone = payload.get("phone") or payload.get("phone_number") or payload.get("mobile") or payload.get("contact_phone")
    phone_e164, phone_err = to_e164(raw_phone, "US")
    if not phone_e164:
        return None, f"phone_{phone_err or 'invalid'}"

    created_at = _parse_created_at(payload)

    email = payload.get("email") or payload.get("email_address")
    state = payload.get("state") or payload.get("region") or payload.get("st")
    zipc = payload.get("zip") or payload.get("postal_code") or payload.get("zipcode")

    consent = extract_consent(payload)

    lead = {
        "lead_id": _lead_id(source, product, phone_e164, created_at, payload),
        "created_at": created_at.isoformat(),
        "source": str(source),
        "product": product,
        "phone_e164": phone_e164,
        "email": (str(email).strip().lower() if email else None),
        "state": (str(state).strip().upper() if state else None),
        "zip": (str(zipc).strip() if zipc else None),
        "consent": consent,
        "payload": payload,
    }
    return lead, None