from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from ..core.errors import ValidationError
from .tcpadnc.evidence_pack import build_evidence_pack


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def validate_lead(lead: Dict[str, Any], allowed_products: list[str], recency_days: int, consent_required: bool, consent_required_fields: list[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    product = lead.get("product")
    if product not in allowed_products:
        return None, "product_not_allowed"

    phone = lead.get("phone_e164")
    if not phone:
        return None, "missing_phone"

    created_at = _parse_iso(str(lead.get("created_at") or ""))
    if not created_at:
        return None, "bad_created_at"

    now = datetime.now(timezone.utc)
    age_days = (now - created_at).total_seconds() / 86400.0
    if age_days > float(recency_days):
        return None, "too_old"

    consent = lead.get("consent") or {}
    if consent_required:
        for f in consent_required_fields:
            v = consent.get(f)
            if v is None or str(v).strip() == "":
                return None, "missing_consent"
        lead["consent"] = build_evidence_pack(consent)

    return lead, None