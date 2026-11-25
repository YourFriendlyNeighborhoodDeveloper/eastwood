"""Per-channel contact authorization for a lead — the quarantine decision.

TCPA consent is specific to the seller it names and to the kind of contact it
describes. A lead is authorized for a channel only when every condition below
holds; anything else is quarantined with machine-readable reasons:

  lead_use_unauthorized    the batch has no written authorization to use the
                           leads (assignment / purchase agreement)
  consent_missing          no consent language at all
  consent_undocumented     language without a capture timestamp, or without a
                           source (URL, certificate, or vendor)
  consent_other_seller     the language does not name your agency
                           (CONSENT_AUTHORIZED_SELLERS) — e.g. consent given to
                           a former employer
  consent_seller_unverifiable  no CONSENT_AUTHORIZED_SELLERS configured
  consent_lacks_ai_voice   language does not cover automated / artificial /
                           prerecorded / AI-voice calls
  consent_lacks_sms        language does not cover text messages (SMS only)
  consent_stale            older than CONSENT_MAX_AGE_DAYS
  source_dnc_flag          the source marked the record do-not-call / opted out
  state_missing / state_unsupported  no calling window can be enforced
  non_us_number            a +1 number outside the US (Canada, Caribbean):
                           not covered by the US registry; other rules apply

  ai_voice     Imogen may call (still subject to DNC scrub, hours, campaign)
  sms          marketing texts may be sent
  manual_call  lead use is authorized and the record carries no DNC flag —
               eligible for the licensed agent's OWN manual review and dialing
               (National DNC and calling hours still apply). Eastwood never
               dials on this basis.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

AI_VOICE_TERMS = (
    "artificial", "prerecorded", "pre-recorded", "pre recorded", "automated", "autodial",
    "auto-dial", "auto dial", "automatic telephone dialing", "artificial intelligence",
    " ai ", "ai-generated", "ai generated", "virtual assistant", "robocall",
)
SMS_TERMS = ("text message", "text messages", "texts", "sms", "mms", "text me", "by text")


@dataclass(frozen=True)
class ConsentEvidence:
    text: str | None
    captured_at: datetime | None
    url: str | None = None
    certificate: str | None = None
    source: str | None = None
    ip: str | None = None


@dataclass
class Authorization:
    ai_voice: bool
    sms: bool
    manual_call: bool
    reasons: list[str] = field(default_factory=list)
    consent_status: str = "none"  # none | undocumented | other_seller | unverified_seller | documented
    seller_named: str | None = None

    def as_json(self, evaluated_at: datetime) -> dict:
        return {
            "ai_voice": self.ai_voice,
            "sms": self.sms,
            "manual_call": self.manual_call,
            "consent_status": self.consent_status,
            "seller_named": self.seller_named,
            "evaluated_at": evaluated_at.isoformat(),
        }


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", text.lower()) + " "


def _names_seller(text: str, sellers: list[str]) -> str | None:
    body = _norm(text)
    for seller in sellers:
        if _norm(seller).strip() and _norm(seller).strip() in body:
            return seller
    return None


def classify(
    *,
    use_authorized: bool,
    consent: ConsentEvidence | None,
    sellers: list[str],
    state: str | None,
    supported_states: frozenset[str],
    source_dnc: bool = False,
    max_age_days: int = 0,
    now: datetime | None = None,
    phone_region: str | None = "US",
) -> Authorization:
    now = now or datetime.now(timezone.utc)
    common: list[str] = []      # block every channel
    consent_block: list[str] = []  # block consent-based channels (ai_voice, sms)

    if not use_authorized:
        common.append("lead_use_unauthorized")
    if source_dnc:
        common.append("source_dnc_flag")
    if phone_region is not None and phone_region != "US":
        common.append("non_us_number")
    if not state:
        common.append("state_missing")
    elif state not in supported_states:
        common.append("state_unsupported")

    status = "none"
    seller = None
    text = (consent.text or "").strip() if consent else ""
    if not text:
        consent_block.append("consent_missing")
    else:
        documented = bool(consent.captured_at) and bool(consent.url or consent.certificate or consent.source)
        if not documented:
            consent_block.append("consent_undocumented")
        seller = _names_seller(text, sellers) if sellers else None
        if not sellers:
            consent_block.append("consent_seller_unverifiable")
            status = "unverified_seller"
        elif not seller:
            consent_block.append("consent_other_seller")
            status = "other_seller"
        elif not documented:
            status = "undocumented"
        else:
            status = "documented"
        if max_age_days and consent.captured_at and (now - consent.captured_at).days > max_age_days:
            consent_block.append("consent_stale")

    body = _norm(text) if text else ""
    ai_terms = any(t in body for t in AI_VOICE_TERMS)
    sms_terms = any(t in body for t in SMS_TERMS)

    ai_reasons = common + consent_block + ([] if (not text or ai_terms) else ["consent_lacks_ai_voice"])
    sms_reasons = common + consent_block + ([] if (not text or sms_terms) else ["consent_lacks_sms"])

    reasons = list(dict.fromkeys(ai_reasons + sms_reasons))
    return Authorization(
        ai_voice=not ai_reasons,
        sms=not sms_reasons,
        manual_call=not common,
        reasons=reasons,
        consent_status=status,
        seller_named=seller,
    )
