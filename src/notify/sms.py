from dataclasses import dataclass
from typing import Any, Dict, Optional
import requests
import re
from ..core.clock import now_iso
from ..core.ids import new_id
from ..contracts.events import TouchEvent

def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")

def normalize_us_e164(raw: str) -> str:
    s = str(raw or "").strip()
    if s.startswith("+"):
        return s
    d = _digits(s)
    if len(d) == 11 and d.startswith("1"):
        return f"+{d}"
    if len(d) == 10:
        return f"+1{d}"
    if len(d) == 11 and not d.startswith("1"):
        d10 = d[:10]
        return f"+1{d10}"
    if len(d) > 11:
        d10 = d[:10]
        return f"+1{d10}"
    return s

@dataclass(frozen=True)
class SmsResult:
    ok: bool
    result: str
    provider_id: Optional[str] = None

class SmsClient:
    def __init__(self, settings):
        self._s = settings

    def send(self, to: str, body: str, lead_id: str, kind: str) -> SmsResult:
        to_e164 = normalize_us_e164(to)
        provider = str(self._s.sms_provider or "").lower().strip()
        if provider != "twilio":
            return self._emit_touch(lead_id, "sms", "skipped_no_provider", {"kind": kind, "to": to_e164})

        tw = self._s.twilio
        sid = str(tw.get("account_sid") or "").strip()
        token = str(tw.get("auth_token") or "").strip()
        frm = normalize_us_e164(str(tw.get("from_number") or "").strip())
        if not sid or not token or not frm or not to_e164 or not body:
            return self._emit_touch(lead_id, "sms", "skipped_missing_config", {"kind": kind, "to": to_e164})

        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        data = {"To": to_e164, "From": frm, "Body": body}

        try:
            r = requests.post(url, data=data, auth=(sid, token), timeout=12)
            ok = 200 <= r.status_code < 300
            j = None
            try:
                j = r.json()
            except Exception:
                j = None
            sid_out = str((j or {}).get("sid") or "")
            res = "sent" if ok else f"error_{r.status_code}"
            meta: Dict[str, Any] = {"kind": kind, "to": to_e164, "from": frm, "http": r.status_code}
            if sid_out:
                meta["provider_id"] = sid_out
            return self._emit_touch(lead_id, "sms", res, meta)
        except Exception as e:
            return self._emit_touch(lead_id, "sms", "exception", {"kind": kind, "to": to_e164, "error": str(e)})

    def _emit_touch(self, lead_id: str, channel: str, result: str, meta: Dict[str, Any]) -> SmsResult:
        ev = TouchEvent(
            event_id=new_id(),
            ts=now_iso(),
            lead_id=lead_id,
            channel=channel,
            result=result,
            meta=dict(meta or {}),
        ).model_dump()
        try:
            from ..core.context import build_context
            ctx = build_context()
            ctx.producer.send(ctx.topics.touches_events, ev)
        except Exception:
            pass
        return SmsResult(ok=result == "sent", result=result, provider_id=str(meta.get("provider_id") or "") or None)