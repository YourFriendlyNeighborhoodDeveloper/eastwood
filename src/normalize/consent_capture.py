from typing import Any, Dict, Optional


def extract_consent(payload: Dict[str, Any]) -> Dict[str, Any]:
    ts = payload.get("consent_ts") or payload.get("opt_in_ts") or payload.get("tcpa_ts") or payload.get("timestamp")
    form_url = payload.get("form_url") or payload.get("landing_page") or payload.get("source_url") or payload.get("page_url")
    disclosure = payload.get("disclosure") or payload.get("tcpa_disclosure") or payload.get("consent_text")
    ip = payload.get("ip") or payload.get("ip_address")
    user_agent = payload.get("user_agent") or payload.get("ua")
    leadid = payload.get("leadid") or payload.get("jornaya_leadid") or payload.get("jornaya") or payload.get("trustedform") or payload.get("trustedform_url")

    out: Dict[str, Any] = {}
    if ts is not None:
        out["ts"] = str(ts)
    if form_url is not None:
        out["form_url"] = str(form_url)
    if disclosure is not None:
        out["disclosure"] = str(disclosure)
    if ip is not None:
        out["ip"] = str(ip)
    if user_agent is not None:
        out["user_agent"] = str(user_agent)
    if leadid is not None:
        out["lead_token"] = str(leadid)
    return out