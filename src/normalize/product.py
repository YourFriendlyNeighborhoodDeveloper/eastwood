from typing import Any, Dict, Optional


def normalize_product(payload: Dict[str, Any]) -> Optional[str]:
    v = payload.get("product") or payload.get("intent") or payload.get("requested_product") or payload.get("lead_type")
    s = (str(v).strip().lower() if v is not None else "")
    blob = " ".join(
        str(payload.get(k) or "")
        for k in ("product", "intent", "requested_product", "lead_type", "notes", "message", "comments", "utm_term", "utm_campaign")
    ).lower()

    if "annuit" in s or "annuit" in blob or "fixed indexed annuity" in blob or "fia" in blob:
        return "annuity"
    if "iul" in s or "indexed universal" in blob or "universal life" in blob:
        return "iul"
    if "final expense" in s or "burial" in blob or "funeral" in blob or "fe" in s:
        return "final_expense"
    return None