"""Prospect-facing quotes — the only path by which a premium reaches a prospect.

Rules:
  * Premiums come exclusively from VERIFIED, effective carrier rate sheets
    (domain.quoting.rate_sheets). The formula estimator and unverified or
    synthetic tables never produce a number a prospect sees. Test sheets are
    usable only outside production and only when ALLOW_TEST_RATE_SHEETS=true.
  * Every option is stored as a quote_records row (quote id + short quote_ref
    + rate sheet + inputs + provenance) BEFORE it can be sent, and outbound
    messages may only mention premiums that match a stored quote for that
    conversation (domain.sms.policy).
  * A quote is a preliminary quote: the premium the carrier's published rates
    give for the stated facts. The final premium depends on the application
    and underwriting; nothing here is bindable.
  * When no quote can be computed, the result carries the missing facts or the
    reasons (no verified sheet, not sold in the state, age outside the rate
    book, ...) — never a substitute number.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from config.settings import get_settings
from db.models import QuoteRecord

from .rate_sheets import SheetQuote, available_faces, quote_from_sheets

DEFAULT_NEED = {"burial": 15_000, "cremation": 10_000}
GENERIC_NEED = 10_000
STEP = 5_000


@dataclass
class ProspectOption:
    label: str
    quote_id: str
    quote_ref: str
    carrier: str
    plan_name: str
    face: int
    monthly_premium: Decimal
    tier: str
    is_test: bool
    within_budget: bool | None

    def as_dict(self) -> dict:
        return {"label": self.label, "quote_id": self.quote_id, "quote_ref": self.quote_ref,
                "carrier": self.carrier, "plan_name": self.plan_name, "face": self.face,
                "monthly_premium": str(self.monthly_premium), "tier": self.tier,
                "is_test_sheet": self.is_test, "within_budget": self.within_budget,
                "preliminary": True, "bindable": False}


@dataclass
class QuoteResult:
    options: list[ProspectOption] = field(default_factory=list)
    unavailable_reasons: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.options)


def allow_test_sheets() -> bool:
    s = get_settings()
    return bool(s.allow_test_rate_sheets) and s.app_env.strip().lower() != "production"


def need_face(desired: int | None, purpose: str | None) -> int:
    if desired and desired > 0:
        return int(desired)
    return DEFAULT_NEED.get((purpose or "").lower(), GENERIC_NEED)


def _round_up(n: int, inc: int = 1000) -> int:
    return ((n + inc - 1) // inc) * inc


async def quote_for_prospect(session, *, tenant_id: str, lead_id, conversation_id=None, product_line: str,
                             state: str | None, age: int | None, sex: str | None, tobacco: bool | None,
                             tier: str | None = None, desired_face: int | None = None, purpose: str | None = None,
                             monthly_budget: float | None = None, mode: str = "monthly",
                             today: date | None = None, max_face_cap: int | None = None) -> QuoteResult:
    missing = [k for k, v in (("age", age), ("state", state), ("tobacco", tobacco)) if v is None]
    if missing:
        return QuoteResult(missing=missing)
    allow_test = allow_test_sheets()

    async def best(face: int) -> tuple[SheetQuote | None, list[str]]:
        quotes, reasons = await quote_from_sheets(
            session, tenant_id=tenant_id, product_line=product_line, state=state, age=age, sex=sex,
            tobacco=tobacco, face=face, tier=tier, mode=mode, today=today, allow_test=allow_test)
        return (quotes[0] if quotes else None), reasons

    # Options are drawn only from face amounts a verified sheet actually quotes
    # (a premium grid lists e.g. $5,000 / $10,000 / $15,000 ...): the smallest
    # offered amount that covers the need, then the next offered amounts up.
    offered = await available_faces(session, tenant_id=tenant_id, product_line=product_line, state=state,
                                    today=today, allow_test=allow_test)

    def snap(face: int) -> int:
        if not offered:
            return face
        return next((f for f in offered if f >= face), offered[-1])

    c_face = snap(_round_up(need_face(desired_face, purpose)))
    if max_face_cap:
        c_face = min(c_face, max_face_cap)
    c, reasons = await best(c_face)
    if c is None:
        if any("needs sex" in r for r in reasons):
            return QuoteResult(missing=["sex"], unavailable_reasons=reasons)
        return QuoteResult(unavailable_reasons=reasons)

    faces = {"C": c_face, "B": snap(c_face + STEP)}
    chosen: dict[str, SheetQuote] = {"C": c}
    b, _ = await best(faces["B"]) if faces["B"] > c_face else (None, [])
    if b:
        chosen["B"] = b
    budget = Decimal(str(monthly_budget)) if monthly_budget else None
    if budget:
        # A: the largest offered face whose exact premium fits the stated budget.
        last_ok = None
        for face in sorted({snap(c_face + k * STEP) for k in range(11)}):
            q, _ = await best(face)
            if q is None:
                break
            if q.premium <= budget:
                last_ok = q
        if last_ok and last_ok.face > max(q.face for q in chosen.values()):
            chosen["A"] = last_ok
    else:
        a_face = snap(c_face + 2 * STEP)
        a, _ = await best(a_face) if a_face > faces["B"] else (None, [])
        if a:
            chosen["A"] = a

    options: list[ProspectOption] = []
    seen: set[int] = set()
    for label in ("C", "B", "A"):
        q = chosen.get(label)
        if q is None or q.face in seen:
            continue
        seen.add(q.face)
        ref = "Q" + secrets.token_hex(3).upper()
        within = (q.premium <= budget) if budget else None
        rec = QuoteRecord(
            lead_id=lead_id, session_id=None, product_line=product_line, face_value=q.face * 100,
            monthly_premium=q.premium, carrier=q.carrier, quote_ref=ref,
            source="rate_sheet_test" if q.is_test else "rate_sheet", rate_sheet_id=q.sheet_id,
            conversation_id=conversation_id,
            quote_data={**q.provenance(), "label": label, "within_budget": within, "preliminary": True,
                        "bindable": False, "quote_type": "carrier_rate_sheet",
                        "quoted_at": datetime.now(timezone.utc).isoformat()},
        )
        session.add(rec)
        await session.flush()
        options.append(ProspectOption(label=label, quote_id=str(rec.id), quote_ref=ref, carrier=q.carrier,
                                      plan_name=q.plan_name, face=q.face, monthly_premium=q.premium,
                                      tier=q.tier, is_test=q.is_test, within_budget=within))
    return QuoteResult(options=options, unavailable_reasons=reasons)
