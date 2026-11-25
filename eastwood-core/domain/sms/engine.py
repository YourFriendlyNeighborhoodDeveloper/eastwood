"""SMS-first engine: outreach, two-way conversation, quoting, booking, reminders.

Flow for one lead (states in sms_conversations.status):

  pending ──opener──▶ contacted ──reply──▶ active ──▶ quoted ──▶ booking ──▶ booked
     │                   │  (no reply: follow-ups, then dormant)       │
     │                   └─ decline / wrong number / STOP ─▶ declined / closed / opted_out
     └─ (calendar full: outreach waits)            active ──(goes quiet: nudges)──▶ dormant

Every inbound text is stored (idempotent on the provider's message id),
understood (domain.sms.nlu, plus the optional model for unclear replies),
and answered with reviewed wording (domain.sms.playbook). Every outbound text
passes, in order: the text policy (no amount that is not a stored quote), the
campaign gate (armed + authorized), the per-lead gate (SMS authorization,
National DNC, suppression, status), the 8am-9pm local window (outreach and
follow-ups always; replies unless SMS_REPLY_OUTSIDE_HOURS), the daily and
campaign budgets, and the rate limits — and is sent at most once per step
(unique step_key). Anything held by a gate is deferred, not dropped.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from config.settings import get_settings
from db.models import Appointment, BookingSlot, Campaign, Lead, QuoteRecord, RateSheet, SmsConversation, SmsMessage
from db.session import session_scope
from domain import spend as spend_mod
from domain.dialing import check_dial_eligibility
from domain.outbound import outbound_permitted, record_contact_decision
from domain.ratelimit import SlidingWindowRate
from domain.scheduling.local import LocalCalendar, SlotUnavailableError
from domain.tcpa import in_tcpa_window, next_tcpa_window_open

from . import playbook, policy
from .booking import book, build_profile, cancel
from .llm import read_with_model
from .nlu import Interpretation, interpret, merge
from .segments import encoding, segments, to_gsm

logger = logging.getLogger(__name__)

ACTIVE = ("pending", "contacted", "active", "waitlist")
FINAL = ("booked", "declined", "opted_out", "closed")


def _hours(spec: str) -> list[float]:
    return [float(x) for x in (spec or "").split(",") if x.strip()]


@dataclass
class Outgoing:
    key: str
    text: str
    step_key: str
    quote_ids: list[str] = field(default_factory=list)


class SmsEngine:
    def __init__(self, telephony, *, clock=None, ai_agent=None) -> None:
        self.tel = telephony
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.rate = SlidingWindowRate()
        # Providers that take a per-message StatusCallback get one, so delivery
        # receipts land on the message (/twilio/sms-status/{id}).
        try:
            self._receipts = telephony is not None and "status_callback" in inspect.signature(telephony.send).parameters
        except (TypeError, ValueError):
            self._receipts = False

    def _deliver(self, to: str, body: str, message_id) -> str:
        base = get_settings().public_base_url.rstrip("/")
        if self._receipts and base:
            return self.tel.send(to, body, status_callback=f"{base}/twilio/sms-status/{message_id}")
        return self.tel.send(to, body)

    # ── helpers ───────────────────────────────────────────────────────────
    def _ctx(self, conv, lead, slots) -> dict:
        s = get_settings()
        product = slots.get("product") or lead.product_line or "final_expense"
        return {"first": (lead.first_name or "there").split()[0].title()[:20], "agency": s.agency_display_name,
                "agent": s.agent_display_name, "product": playbook.PRODUCT_PHRASE.get(product, "life insurance"),
                "product_short": playbook.PRODUCT_SHORT.get(product, "life insurance")}

    def _say(self, conv, lead, slots, key: str, step, **kw) -> str:
        ctx = {**self._ctx(conv, lead, slots), **kw}
        template, variant = playbook.pick(key, str(conv.id), step, avoid=slots.get("last_variant"))
        if key in playbook.ONE_SEGMENT_KEYS:
            # Sent to every lead: use a one-segment variant whenever one fits
            # (the picked one if it does, else the fullest one that does).
            rendered = [(t, f"{key}#{i}", to_gsm(t.format(**ctx))) for i, t in enumerate(playbook.T[key])]
            fits = [r for r in rendered if segments(r[2]) == 1]
            if fits and variant not in {r[1] for r in fits}:
                template, variant, _ = max(fits, key=lambda r: len(r[2]))
            elif not fits:
                template, variant, _ = min(rendered, key=lambda r: len(r[2]))
        slots["last_variant"] = variant
        text = template.format(**ctx)
        return text[0].upper() + text[1:] if text else text

    @staticmethod
    def _allowed_amounts(slots: dict) -> set[Decimal]:
        out: set[Decimal] = set()
        for o in slots.get("options") or []:
            out.add(Decimal(str(o["face"])))
            out.add(Decimal(str(o["premium"])))
        return out

    async def _tier_questions(self, db, conv, lead, slots) -> list[dict]:
        from domain.quoting.prospect import allow_test_sheets

        now = self.clock().date()
        q = select(RateSheet).where(RateSheet.tenant_id == conv.tenant_id, RateSheet.status == "verified",
                                    RateSheet.product_line == (slots.get("product") or "final_expense"),
                                    RateSheet.effective_date <= now)
        seen, out = set(), []
        for sheet in (await db.execute(q)).scalars():
            if sheet.is_test and not allow_test_sheets():
                continue
            if (sheet.expires_date and sheet.expires_date <= now) or lead.state not in (sheet.states or []):
                continue
            for item in sheet.tier_questions or []:
                if item["key"] not in seen:
                    seen.add(item["key"])
                    out.append(item)
        return out

    def _pending(self, conv, lead, slots, step) -> tuple[str, str] | None:
        if not slots.get("face"):
            return "face", self._say(conv, lead, slots, "ask_face", step)
        if slots.get("age") is None:
            return "age", self._say(conv, lead, slots, "ask_age", step)
        if slots.get("tobacco") is None:
            return "tobacco", self._say(conv, lead, slots, "ask_tobacco", step)
        if slots.get("sex") is None:
            return "sex", self._say(conv, lead, slots, "ask_sex", step)
        health = slots.get("health") or {}
        for q in slots.get("tier_questions") or []:
            if q["key"] not in health:
                return f"health:{q['key']}", self._say(conv, lead, slots, "ask_health", step, question=q["text"])
        if not slots.get("asked_budget"):
            slots["asked_budget"] = True
            return "budget", self._say(conv, lead, slots, "ask_budget", step)
        return None

    # ── inbound ───────────────────────────────────────────────────────────
    async def handle_inbound(self, *, phone: str, body: str, provider_sid: str, num_segments: int = 1) -> dict:
        s = get_settings()
        now = self.clock()
        post: list = []
        async with session_scope() as db:
            conv = (await db.execute(select(SmsConversation).where(
                SmsConversation.tenant_id == s.default_tenant_id, SmsConversation.phone_e164 == phone,
            ).order_by(SmsConversation.updated_at.desc()).limit(1))).scalars().first()
            if conv is None:
                return {"handled": False, "reason": "no_conversation"}
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"sms:{conv.id}"})
            await db.refresh(conv)
            msg = SmsMessage(conversation_id=conv.id, direction="in", body=body[:1600], segments=max(num_segments, 1),
                             encoding=encoding(body), provider_sid=provider_sid[:64], status="received")
            try:
                async with db.begin_nested():  # added inside the savepoint so a duplicate rolls back cleanly
                    db.add(msg)
                    await db.flush()
            except IntegrityError:
                return {"handled": False, "reason": "duplicate"}
            lead = await db.get(Lead, conv.lead_id)
            in_cost = msg.segments * s.cost_sms_inbound_per_segment_usd
            await spend_mod.add_lines(db, tenant_id=conv.tenant_id, campaign_id=conv.campaign_id,
                                      reference=f"sms-in:{msg.id}", lines=[("sms_in", float(msg.segments),
                                                                             s.cost_sms_inbound_per_segment_usd)])
            msg.cost_usd = in_cost
            prev_inbound = conv.last_inbound_at
            conv.inbound_count += 1
            conv.last_inbound_at = now
            conv.cost_usd = float(conv.cost_usd or 0) + in_cost
            if conv.status in ("opted_out", "declined", "closed"):
                return {"handled": True, "replied": 0, "reason": conv.status}
            slots = dict(conv.slots or {})
            it = interpret(body, expecting=slots.get("expecting"), offered=len(slots.get("offered") or []),
                           today=now.date())
            if it.unclear or it.question == "other":
                reading = await read_with_model(body, expecting=slots.get("expecting"), last_question=slots.get("last_prompt"))
                if reading:
                    it = merge(it, reading.interpretation)
                    msg.llm_used, msg.tokens_in, msg.tokens_out = True, reading.tokens_in, reading.tokens_out
                    conv.llm_tokens_in += reading.tokens_in
                    conv.llm_tokens_out += reading.tokens_out
                    await spend_mod.add_lines(db, tenant_id=conv.tenant_id, campaign_id=conv.campaign_id,
                                              reference=f"llm:{msg.id}", lines=[("llm", 1.0, reading.cost_usd)])
                    conv.cost_usd = float(conv.cost_usd) + reading.cost_usd
            msg.intent = (",".join(sorted(it.intents)) or it.objection or it.question
                          or ("values" if it.values else "unclear"))[:48]
            quiet_since = (now - prev_inbound) if prev_inbound else None
            outgoing = await self._decide(db, conv, lead, slots, it, now, post,
                                          returning=bool(quiet_since and quiet_since > timedelta(hours=20))
                                          or conv.status == "dormant")
            sent = 0
            for o in outgoing:
                sent += (await self._send(db, conv, lead, o, reply=True, now=now)) == "sent"
        for fn in post:
            try:
                await fn()
            except Exception:
                logger.exception("post-commit action failed")
        return {"handled": True, "replied": sent, "intent": msg.intent}

    async def _decide(self, db, conv, lead, slots, it: Interpretation, now, post, *, returning: bool) -> list[Outgoing]:
        s = get_settings()
        n = conv.inbound_count
        step = f"r{n}"
        parts: list[str] = []
        out: list[Outgoing] = []

        def finish(extra: list[Outgoing] | None = None, key: str = "reply") -> list[Outgoing]:
            if parts:
                out.insert(0, Outgoing(key, " ".join(p.strip() for p in parts if p), f"{step}:a"))
            if extra:
                out.extend(extra)
            # one nudge timer while we wait for an answer
            if conv.status in ("active",) and slots.get("expecting") and slots.get("expecting") != "booked":
                conv.next_action, conv.next_action_at = "stall_1", now + timedelta(hours=_hours(s.sms_stall_hours)[0])
            elif conv.status in FINAL or conv.status == "handoff":
                conv.next_action, conv.next_action_at = None, None
            conv.slots = dict(slots)
            return out

        # ── terminal replies
        if "wrong_number" in it.intents:
            await self._suppress(db, conv, lead, "wrong_number")
            conv.status = "closed"
            parts.append(self._say(conv, lead, slots, "wrong_number", step))
            return finish(key="closing")
        if "decline" in it.intents or (slots.get("expecting") in ("opt_in", "call_offer") and "no" in it.intents
                                       and not it.values and not it.question):
            await self._suppress(db, conv, lead, "sms_declined")
            conv.status = "declined"
            slots["expecting"] = None
            parts.append(self._say(conv, lead, slots, "declined", step))
            return finish(key="closing")

        # ── after booking
        if conv.status == "booked":
            if "reschedule" in it.intents:
                await cancel(db, conv.appointment_id, outcome="rescheduled")
                conv.appointment_id, conv.status = None, "active"
                await self._offer_times(db, conv, lead, slots, parts, now, step, again=True)
                return finish()
            if "cancel" in it.intents:
                await cancel(db, conv.appointment_id, outcome="cancelled")
                conv.appointment_id, conv.status = None, "active"
                slots["expecting"] = None
                parts.append(self._say(conv, lead, slots, "cancelled", step))
                return finish()
            if it.question:
                parts.append(self._faq(conv, lead, slots, it, step))
            if not ({"thanks", "yes"} & it.intents) or it.question:
                appt = await db.get(Appointment, conv.appointment_id) if conv.appointment_id else None
                slot = await db.get(BookingSlot, appt.slot_id) if appt else None
                if slot:
                    parts.append(self._say(conv, lead, slots, "after_booked", step, when=playbook.when(slot.starts_at, lead.state)))
            return finish()

        # ── engagement
        first_engagement = conv.status in ("pending", "contacted")
        if conv.status in ("pending", "contacted", "dormant", "waitlist"):
            conv.status = "active"
        if first_engagement and not slots.get("tier_questions_loaded"):
            slots["tier_questions"] = await self._tier_questions(db, conv, lead, slots)
            slots["tier_questions_loaded"] = True

        for key, val in it.values.items():
            if key.startswith("health:"):
                slots.setdefault("health", {})[key[7:]] = bool(val)
            elif key in ("age", "face", "budget", "tobacco", "sex", "purpose"):
                slots[key] = val
            elif key == "face_unsure":
                from domain.quoting.prospect import need_face
                slots["face"] = need_face(None, slots.get("purpose"))
                slots["face_suggested"] = True
        if it.objection:
            slots.setdefault("objections", [])
            if it.objection not in slots["objections"]:
                slots["objections"].append(it.objection)
        if it.question == "other" and it.question_text:
            slots.setdefault("questions", []).append(it.question_text)

        if it.question and it.question != "cost":
            parts.append(self._faq(conv, lead, slots, it, step))
        if it.objection == "trust":
            parts.append(self._say(conv, lead, slots, "objection_trust", step))

        exp = slots.get("expecting")
        # ── call requests
        if "wants_call" in it.intents:
            campaign = await db.get(Campaign, conv.campaign_id) if conv.campaign_id else None
            if it.values.get("call_now") and s.agent_notification_phone_e164:
                conv.status = "handoff"
                post.append(self._alert_agent(conv, lead, "asked for a call now"))
                parts.append(self._say(conv, lead, slots, "handoff_call", step))
                return finish()
            if campaign and campaign.escalate_to_voice and (lead.contact_authorization or {}).get("ai_voice"):
                from repos.repos import DialQueueRepo
                await DialQueueRepo(db).enqueue(lead_id=lead.id, tenant_id=conv.tenant_id, priority=1.0,
                                                scheduled_at=now, max_attempts=1, campaign_id=campaign.id)
                conv.status = "handoff"
                parts.append(self._say(conv, lead, slots, "voice_escalation", step))
                return finish()
            await self._offer_times(db, conv, lead, slots, parts, now, step)
            return finish()

        # ── objections
        if it.objection == "not_now" and not ({"yes"} & it.intents) and not it.values:
            parts.append(self._say(conv, lead, slots, "snooze", step))
            conv.next_action, conv.next_action_at = "snooze", now + timedelta(hours=72)
            conv.slots = dict(slots)
            return [Outgoing("reply", " ".join(parts), f"{step}:a")]
        if it.objection in ("spouse", "have_coverage"):
            parts.append(self._say(conv, lead, slots, f"objection_{it.objection}", step))
            slots["expecting"] = "call_offer"
            return finish()
        if it.objection == "price":
            options = slots.get("options") or []
            if options:
                low = min(options, key=lambda o: Decimal(str(o["premium"])))
                slots["lower_quote_id"] = low["quote_id"]
                parts.append(self._say(conv, lead, slots, "lower_option", step,
                                       option=f"{playbook.face(low['face'])} for {playbook.money(Decimal(str(low['premium'])))}/mo"))
                slots["expecting"] = "lower_offer"
                return finish()

        # ── choices
        if exp == "option" and it.values.get("option_choice"):
            idx = int(it.values["option_choice"]) - 1
            options = slots.get("options") or []
            if 0 <= idx < len(options):
                slots["selected_quote_id"] = options[idx]["quote_id"]
                await self._offer_times(db, conv, lead, slots, parts, now, step)
                return finish()
        if exp == "lower_offer" and "yes" in it.intents:
            slots["selected_quote_id"] = slots.get("lower_quote_id")
            await self._offer_times(db, conv, lead, slots, parts, now, step)
            return finish()
        if exp == "call_offer" and ("yes" in it.intents or it.values.get("option_choice")):
            await self._offer_times(db, conv, lead, slots, parts, now, step)
            return finish()
        if exp == "time":
            choice = it.values.get("time_choice")
            if choice:
                await self._book(db, conv, lead, slots, parts, now, step, int(choice), post)
                return finish()
            await self._offer_times(db, conv, lead, slots, parts, now, step, again=True)
            return finish()

        # ── a question (or chit-chat) before saying yes: answer, then ask again
        if exp == "opt_in" and "yes" not in it.intents and not it.values:
            parts.append(self._say(conv, lead, slots, "confirm_opt_in", step))
            return finish()

        # ── qualifying → quote
        if not slots.get("options") and not slots.get("unavailable"):
            pending = self._pending(conv, lead, slots, step)
            if pending:
                slots["expecting"], slots["last_prompt"] = pending
                if first_engagement and exp in (None, "opt_in"):
                    parts.append(self._say(conv, lead, slots, "start_qualifying", step, pending=pending[1]))
                elif returning:
                    parts.append(self._say(conv, lead, slots, "welcome_back", step, pending=pending[1]))
                elif it.unclear:
                    self._clarify(conv, lead, slots, parts, step, pending[1], post)
                else:
                    parts.append(("" if it.question else self._say(conv, lead, slots, "ack", step) + " ") + pending[1])
                return finish()
            quote_msg = await self._quote(db, conv, lead, slots, step)
            return finish([quote_msg] if quote_msg else None)

        if exp == "option":
            n_opts = len(slots.get("options") or []) or 3
            self._clarify(conv, lead, slots, parts, step, f"Reply {playbook.choices(n_opts)} to pick an option.", post)
        elif exp == "call_offer" and not parts:
            self._clarify(conv, lead, slots, parts, step, "Would you like a short call with "
                          + s.agent_display_name + "? Reply YES or NO.", post)
        return finish()

    def _clarify(self, conv, lead, slots, parts, step, pending: str, post) -> None:
        """Ask again, at most twice for the same question. A third miss means
        the texts are not working for this person: the agent takes over."""
        expecting = slots.get("expecting")
        count = int(slots.get("clarify_count") or 0) + 1 if slots.get("clarify_for") == expecting else 1
        slots["clarify_for"], slots["clarify_count"] = expecting, count
        if count >= 3:
            slots["expecting"] = None
            conv.status, conv.next_action, conv.next_action_at = "handoff", None, None
            parts.append(self._say(conv, lead, slots, "handoff_call", step))
            post.append(self._alert_agent(conv, lead, f"could not answer '{expecting}' by text; please call"))
            return
        parts.append(self._say(conv, lead, slots, "clarify", step, pending=pending))

    def _faq(self, conv, lead, slots, it: Interpretation, step) -> str:
        key = {"who": "faq_who", "how_got_number": "faq_how_got_number", "is_ai": "faq_is_ai",
               "exam": "faq_exam", "what_covers": "faq_what_covers", "cost": "faq_cost"}.get(it.question or "", "faq_unknown")
        if key == "faq_what_covers" and (slots.get("product") or lead.product_line) not in (None, "final_expense"):
            key = "faq_unknown"
        return self._say(conv, lead, slots, key, step)

    async def _quote(self, db, conv, lead, slots, step) -> Outgoing | None:
        from domain.quoting.prospect import quote_for_prospect

        tier = None
        for q in slots.get("tier_questions") or []:
            if (slots.get("health") or {}).get(q["key"]):
                tier = q["yes_tier"]
                break
        slots["tier"] = tier
        product = slots.get("product") or lead.product_line or "final_expense"
        if product not in ("final_expense", "life"):
            product = "final_expense"
        res = await quote_for_prospect(
            db, tenant_id=conv.tenant_id, lead_id=lead.id, conversation_id=conv.id, product_line=product,
            state=lead.state, age=slots.get("age"), sex=slots.get("sex"), tobacco=slots.get("tobacco"), tier=tier,
            desired_face=slots.get("face"), purpose=slots.get("purpose"), monthly_budget=slots.get("budget") or None)
        if res.missing:
            slots["unavailable"] = [f"missing {', '.join(res.missing)}"]
        if not res.ok:
            slots["unavailable"] = slots.get("unavailable") or [r for r in res.unavailable_reasons][:3] or ["no rate"]
            slots["expecting"] = "call_offer"
            return Outgoing("quote_unavailable", self._say(conv, lead, slots, "quote_unavailable", step), f"{step}:quote")
        options = sorted(res.options, key=lambda o: o.face)
        slots["options"] = [{"n": i, "quote_id": o.quote_id, "quote_ref": o.quote_ref, "face": o.face,
                             "premium": str(o.monthly_premium), "carrier": o.carrier, "plan": o.plan_name,
                             "tier": o.tier, "is_test": o.is_test} for i, o in enumerate(options, start=1)]
        carriers = {o.carrier for o in options}
        if len(carriers) == 1:
            head = self._say(conv, lead, slots, "quote_intro", step, carrier=options[0].carrier, plan=options[0].plan_name)
            lines = [f"{i}) {playbook.face(o.face)}: {playbook.money(o.monthly_premium)}/mo" for i, o in enumerate(options, 1)]
        else:
            head = "Here are your preliminary monthly rates:"
            lines = [f"{i}) {playbook.face(o.face)} with {o.carrier}: {playbook.money(o.monthly_premium)}/mo"
                     for i, o in enumerate(options, 1)]
        tail = self._say(conv, lead, slots, "quote_outro" if len(options) > 1 else "quote_outro_single", step,
                         choices=playbook.choices(len(options)))
        prefix = "TEST RATES, not a real carrier. " if any(o.is_test for o in options) else ""
        slots["expecting"] = "option" if len(options) > 1 else "call_offer"
        conv.stage = "quoted"
        return Outgoing("quote", prefix + head + "\n" + "\n".join(lines) + "\n" + tail, f"{step}:quote",
                        quote_ids=[o.quote_id for o in options])

    async def _offer_times(self, db, conv, lead, slots, parts, now, step, again: bool = False) -> None:
        s = get_settings()
        calendar = LocalCalendar.from_settings(s)
        if calendar is None:
            conv.status = "handoff"
            parts.append(self._say(conv, lead, slots, "handoff_call", step))
            return
        exclude = set(slots.get("offered") or []) if again else set()
        times = await self._pick_times(db, conv, calendar, now, exclude)
        if not times:
            slots["expecting"] = None
            slots.pop("offered", None)      # nothing is on offer while the calendar is full
            conv.status = "waitlist"
            conv.next_action, conv.next_action_at = "retry_booking", now + timedelta(hours=24)
            parts.append(self._say(conv, lead, slots, "calendar_full", step))
            return
        slots["offered"] = times
        slots["expecting"] = "time"
        conv.stage = "booking"
        lines = playbook.slot_lines([datetime.fromisoformat(t) for t in times], lead.state)
        parts.append(self._say(conv, lead, slots, "offer_times_again" if again else "offer_times", step, times=lines,
                               choices=playbook.choices(len(times))))

    async def _pick_times(self, db, conv, calendar, now, exclude: set) -> list[str]:
        """Three open times for this prospect. Times offered to another prospect
        in the last SMS_OFFER_HOLD_MINUTES are held back (when a campaign burst
        has many people choosing at once, offering everyone the same three
        earliest slots makes them collide over and over), and the three are
        spread at least 90 minutes apart so the choice is a real one."""
        s = get_settings()
        free = [t for t in await calendar.available_times(db, conv.tenant_id, now=now, limit=48) if t not in exclude]
        if not free:
            free = await calendar.available_times(db, conv.tenant_id, now=now, limit=3)
        since = datetime.now(timezone.utc) - timedelta(minutes=s.sms_offer_hold_minutes)  # updated_at is wall-clock
        held: set[str] = set()
        for (offered,) in (await db.execute(
                select(SmsConversation.slots["offered"]).where(
                    SmsConversation.tenant_id == conv.tenant_id, SmsConversation.id != conv.id,
                    SmsConversation.status.in_(ACTIVE), SmsConversation.updated_at >= since,
                    SmsConversation.slots["expecting"].astext == "time"))).all():
            held.update(offered or [])
        pool = [t for t in free if t not in held] or free
        # Holds are only visible once the other conversation commits, so two
        # prospects answering in the same second would still get identical
        # offers. Each conversation therefore draws from the earliest open
        # times in its own (stable) order: simultaneous choosers rarely overlap.
        head = pool[:16]
        rng = random.Random(f"{conv.id}:{len(exclude)}")
        rng.shuffle(head)
        pool = head + pool[16:]
        picked: list[str] = []
        for t in pool:
            at = datetime.fromisoformat(t)
            if all(abs((at - datetime.fromisoformat(p)).total_seconds()) >= 90 * 60 for p in picked):
                picked.append(t)
            if len(picked) == 3:
                break
        for t in pool:                      # a nearly full calendar: fill with whatever is left
            if len(picked) == 3:
                break
            if t not in picked:
                picked.append(t)
        return sorted(picked, key=datetime.fromisoformat)

    async def _book(self, db, conv, lead, slots, parts, now, step, choice: int, post) -> None:
        offered = slots.get("offered") or []
        if not 1 <= choice <= len(offered):
            await self._offer_times(db, conv, lead, slots, parts, now, step, again=True)
            return
        start = datetime.fromisoformat(offered[choice - 1])
        calendar = LocalCalendar.from_settings(get_settings())
        try:
            async with db.begin_nested():
                appt = await book(db, conv, lead, start, calendar, now=now)
        except (SlotUnavailableError, IntegrityError):
            slots["slot_taken_count"] = int(slots.get("slot_taken_count") or 0) + 1
            if slots["slot_taken_count"] >= 3:
                # Three picks in a row were gone: stop offering and let the agent
                # set the time personally rather than keep the prospect chasing.
                slots["expecting"] = None
                conv.status, conv.next_action, conv.next_action_at = "handoff", None, None
                parts.append(self._say(conv, lead, slots, "handoff_call", step))
                post.append(self._alert_agent(conv, lead, "wants an appointment; three chosen times were taken"))
                return
            times = await self._pick_times(db, conv, calendar, now, set(offered))
            if not times:
                await self._offer_times(db, conv, lead, slots, parts, now, step, again=True)
                return
            slots["offered"] = times
            parts.append(self._say(conv, lead, slots, "slot_taken", step, times=playbook.slot_lines(
                [datetime.fromisoformat(t) for t in times], lead.state), choices=playbook.choices(len(times))))
            return
        slots.pop("slot_taken_count", None)
        conv.status, conv.stage, conv.appointment_id = "booked", "booked", appt.id
        slots["expecting"] = "booked"
        lead.status = "appointment_set"
        parts.append(self._say(conv, lead, slots, "booked", step, when=playbook.when(start, lead.state)))
        appointment_id = appt.id

        async def _brief():
            from db.models import HandoffProfile
            from domain.notifications import AppointmentNotificationService

            async with session_scope() as s2:
                hp = (await s2.execute(select(HandoffProfile).where(HandoffProfile.appointment_id == appointment_id))).scalars().first()
            if hp is not None:
                svc = AppointmentNotificationService(self.tel, get_settings().agent_notification_phone_e164)
                await asyncio.to_thread(svc.send_agent_brief, hp.profile, appointment_id=str(appointment_id))
        post.append(_brief)

    def _alert_agent(self, conv, lead, what: str):
        async def _go():
            dest = get_settings().agent_notification_phone_e164
            if dest and self.tel:
                name = " ".join(x for x in (lead.first_name, lead.last_name) if x) or "A prospect"
                await asyncio.to_thread(self.tel.send, dest, f"EASTWOOD: {name} {lead.phone_e164} {what}. "
                                                          f"Conversation {str(conv.id)[:8]}.")
        return _go

    async def _suppress(self, db, conv, lead, reason: str) -> None:
        from repos.repos import SuppressionRepo

        repo = SuppressionRepo(db)
        if not await repo.is_suppressed(conv.tenant_id, lead.phone_e164):
            await repo.add(conv.tenant_id, lead.phone_e164, reason=reason, notes=f"sms conversation {conv.id}")
        conv.next_action, conv.next_action_at = None, None

    # ── outbound ──────────────────────────────────────────────────────────
    async def _send(self, db, conv, lead, o: Outgoing, *, reply: bool, now) -> str:
        s = get_settings()
        body = to_gsm(o.text).strip()
        slots = dict(conv.slots or {})
        problems = policy.validate(body, allowed_amounts=self._allowed_amounts(slots),
                                   first_message=conv.outbound_count == 0 and o.key == "opener",
                                   agency=s.agency_display_name)
        if problems:
            logger.error("SMS blocked by policy (%s) conversation=%s step=%s", problems, conv.id, o.step_key)
            record_contact_decision("sms", "policy_blocked")
            return "policy_blocked"
        if self.tel is None:
            record_contact_decision("sms", "skipped_no_provider")
            return "no_provider"
        if conv.outbound_count >= s.sms_max_outbound_per_conversation and o.key not in ("closing", "reminder"):
            # Cost guard: no conversation runs away, whatever the cause.
            logger.warning("SMS conversation cap (%s) reached conversation=%s step=%s; handed to the agent",
                           s.sms_max_outbound_per_conversation, conv.id, o.step_key)
            record_contact_decision("sms", "conversation_cap")
            if conv.status not in FINAL:
                conv.status, conv.next_action, conv.next_action_at = "handoff", None, None
            return "conversation_cap"
        permitted, _ = await outbound_permitted(db)
        if not permitted:
            return self._defer(conv, slots, o, now + timedelta(minutes=5)) if reply else "halted"
        eligible, reason = await check_dial_eligibility(db, lead, channel="sms")
        # The one-line confirmation of a decline / wrong number goes to the number
        # this reply just suppressed; nothing else ever does.
        if not eligible and reason == "suppressed" and o.key == "closing":
            eligible = True
        if not eligible:
            record_contact_decision("sms", "blocked")
            if reason == "suppressed" and conv.status not in FINAL:
                conv.status, conv.next_action, conv.next_action_at = "opted_out", None, None
            return "blocked"
        if not in_tcpa_window(lead.state, now) and not (reply and s.sms_reply_outside_hours):
            opens = next_tcpa_window_open(lead.state, now)
            if reply:
                return self._defer(conv, slots, o, opens)
            conv.next_action_at = opens  # outreach / follow-ups keep their own schedule
            return "quiet_hours"
        segs = segments(body)
        cost = segs * s.cost_sms_per_segment_usd
        if not await spend_mod.sms_within_budget(db, tenant_id=conv.tenant_id, campaign_id=conv.campaign_id,
                                                 cost=cost, now=now):
            record_contact_decision("sms", "budget_deferred")
            tomorrow = spend_mod.next_day_start(now, s.calendly_default_timezone)
            if reply:
                return self._defer(conv, slots, o, tomorrow)
            conv.next_action_at = tomorrow
            return "budget_deferred"
        campaign = await db.get(Campaign, conv.campaign_id) if conv.campaign_id else None
        if not self.rate.allow(None, s.sms_messages_per_minute, now) or (
                campaign and not self.rate.allow(campaign.id, campaign.messages_per_minute, now)):
            return self._defer(conv, slots, o, now + timedelta(seconds=20)) if reply else "rate_limited"
        msg = SmsMessage(conversation_id=conv.id, direction="out", body=body, segments=segs, encoding=encoding(body),
                         status="sending", step_key=o.step_key[:64], intent=o.key[:48],
                         quote_id=o.quote_ids[0] if o.quote_ids else None, cost_usd=cost)
        try:
            async with db.begin_nested():
                db.add(msg)
                await db.flush()
        except IntegrityError:
            return "duplicate"
        try:
            sid = await asyncio.to_thread(self._deliver, lead.phone_e164, body, msg.id)
        except Exception:
            logger.exception("SMS send failed conversation=%s", conv.id)
            msg.status = "failed"
            return "failed"
        msg.provider_sid, msg.status = str(sid)[:64], "sent"
        self.rate.record(None, now)
        if campaign:
            self.rate.record(campaign.id, now)
        await spend_mod.add_lines(db, tenant_id=conv.tenant_id, campaign_id=conv.campaign_id,
                                  reference=f"sms-out:{msg.id}", lines=[("sms", float(segs), s.cost_sms_per_segment_usd)])
        conv.outbound_count += 1
        conv.last_outbound_at = now
        conv.cost_usd = float(conv.cost_usd or 0) + cost
        if o.quote_ids:
            await db.execute(update(QuoteRecord).where(QuoteRecord.id.in_(o.quote_ids),
                                                       QuoteRecord.communicated_at.is_(None)).values(communicated_at=now))
        record_contact_decision("sms", "sent")
        return "sent"

    def _defer(self, conv, slots, o: Outgoing, when: datetime) -> str:
        slots = dict(conv.slots or {})
        pending = [d for d in slots.get("deferred") or [] if d["step_key"] != o.step_key]
        pending.append({"key": o.key, "text": o.text, "step_key": o.step_key, "quote_ids": o.quote_ids})
        slots["deferred"] = pending
        conv.slots = slots
        conv.next_action, conv.next_action_at = "deferred", when
        return "deferred"

    # ── scheduled work ─────────────────────────────────────────────────────
    async def run_due(self, *, limit: int = 100) -> dict:
        s = get_settings()
        now = self.clock()
        counts: dict = {}
        async with session_scope() as db:
            permitted, _ = await outbound_permitted(db)
            if not permitted:
                return {"held": "outbound_not_permitted"}
            calendar = LocalCalendar.from_settings(s)
            calendar_full = bool(calendar) and s.pause_dialing_when_calendar_full and not await calendar.available_times(
                db, s.default_tenant_id, now=now, limit=1)
            q = (select(SmsConversation.id, SmsConversation.next_action)
                 .join(Campaign, Campaign.id == SmsConversation.campaign_id, isouter=True)
                 .where(SmsConversation.tenant_id == s.default_tenant_id,   # the tenant the gate checked
                        SmsConversation.next_action_at <= now, SmsConversation.next_action.is_not(None),
                        # A deferred reply was already decided (e.g. "You're booked", held
                        # a few seconds by the rate limit) and must go out whatever the
                        # conversation's status is now; every gate is re-checked at send.
                        SmsConversation.status.in_(ACTIVE + ("dormant",)) | (SmsConversation.next_action == "deferred"),
                        (Campaign.id.is_(None)) | (Campaign.status == "active")))
            if calendar_full:
                # New outreach waits for an open slot. It is left out of the
                # batch entirely: held openers are the oldest rows and would
                # otherwise starve follow-ups, nudges and deferred replies.
                held = (await db.execute(q.with_only_columns(func.count()).where(
                    SmsConversation.next_action == "outreach"))).scalar_one()
                if held:
                    counts["outreach_held_calendar_full"] = held
                q = q.where(SmsConversation.next_action != "outreach")
            due = list((await db.execute(q.order_by(SmsConversation.next_action_at).limit(limit))).all())
        for conv_id, action in due:
            result = await self._run_one(conv_id, now)
            counts[result] = counts.get(result, 0) + 1
            if result == "rate_limited":
                break
        counts.update(await self._reminders(now))
        return counts

    async def _run_one(self, conv_id, now) -> str:
        s = get_settings()
        async with session_scope() as db:
            await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"sms:{conv_id}"})
            conv = await db.get(SmsConversation, conv_id)
            if conv is None or conv.next_action is None or (conv.next_action_at and conv.next_action_at > now):
                return "skipped"
            lead = await db.get(Lead, conv.lead_id)
            slots = dict(conv.slots or {})
            action = conv.next_action
            step = f"{action}:{conv.inbound_count}"
            if action == "outreach":
                campaign = await db.get(Campaign, conv.campaign_id) if conv.campaign_id else None
                if campaign and campaign.daily_message_cap:
                    sent_today = (await db.execute(select(func.count(SmsMessage.id)).join(
                        SmsConversation, SmsConversation.id == SmsMessage.conversation_id).where(
                        SmsConversation.campaign_id == campaign.id, SmsMessage.direction == "out",
                        SmsMessage.status == "sent",
                        SmsMessage.created_at >= spend_mod.day_start_utc(now, s.calendly_default_timezone)))).scalar_one()
                    if sent_today >= campaign.daily_message_cap:
                        return "daily_cap"
                slots["product"] = lead.product_line if lead.product_line in ("final_expense", "life") else "final_expense"
                for key, val in (("age", lead.age), ("sex", lead.sex), ("tobacco", lead.tobacco)):
                    if val is not None and key not in slots:
                        slots[key] = val
                slots["expecting"] = "opt_in"
                conv.slots = slots
                result = await self._send(db, conv, lead, Outgoing("opener", self._say(conv, lead, slots, "opener", "o"),
                                                                   "outreach"), reply=False, now=now)
                if result == "sent":
                    conv.status = "contacted"
                    conv.slots = dict(slots)
                    conv.next_action, conv.next_action_at = "followup_1", now + timedelta(hours=_hours(s.sms_followup_hours)[0])
                return result
            if action in ("followup_1", "followup_2"):
                if conv.status != "contacted":
                    conv.next_action = None
                    return "skipped"
                hours = _hours(s.sms_followup_hours)
                result = await self._send(db, conv, lead, Outgoing(action, self._say(conv, lead, slots, action, step),
                                                                   action), reply=False, now=now)
                if result == "sent":
                    if action == "followup_1" and len(hours) > 1:
                        conv.next_action = "followup_2"
                        conv.next_action_at = (conv.last_outbound_at or now) + timedelta(hours=hours[1] - hours[0])
                    else:
                        conv.status, conv.next_action, conv.next_action_at = "dormant", None, None
                conv.slots = dict(conv.slots or slots)
                return result
            if action in ("stall_1", "stall_2", "snooze"):
                if conv.status not in ("active",):
                    conv.next_action = None
                    return "skipped"
                key = "stall_2" if action == "stall_2" else "stall_1"
                body = self._say(conv, lead, slots, key, step, pending=slots.get("last_prompt") or "")
                result = await self._send(db, conv, lead, Outgoing(key, body, f"{action}:{conv.inbound_count}"),
                                          reply=False, now=now)
                if result == "sent":
                    if key == "stall_1":
                        stall = _hours(s.sms_stall_hours)
                        conv.next_action = "stall_2"
                        conv.next_action_at = now + timedelta(hours=(stall[1] - stall[0]) if len(stall) > 1 else 48)
                    else:
                        conv.status, conv.next_action, conv.next_action_at = "dormant", None, None
                conv.slots = dict(conv.slots or slots)
                return result
            if action == "retry_booking":
                # Waitlisted: text only when a time has actually opened up. While
                # the calendar stays full this re-checks daily and sends nothing.
                parts: list[str] = []
                await self._offer_times(db, conv, lead, slots, parts, now, f"retry:{conv.inbound_count}:{now:%Y%m%d}")
                conv.slots = dict(slots)
                if slots.get("expecting") == "time" and slots.get("offered"):
                    conv.status = "active"
                    conv.next_action, conv.next_action_at = "stall_1", now + timedelta(hours=_hours(s.sms_stall_hours)[0])
                    return await self._send(db, conv, lead, Outgoing("offer_times", " ".join(parts),
                                                                     f"retry:{conv.inbound_count}:{now:%Y%m%d}"),
                                            reply=False, now=now)
                return "calendar_full"
            if action == "deferred":
                pending = list(slots.get("deferred") or [])
                slots["deferred"] = []
                conv.slots = slots
                conv.next_action, conv.next_action_at = None, None
                results = [await self._send(db, conv, lead, Outgoing(d["key"], d["text"], d["step_key"], d.get("quote_ids") or []),
                                            reply=True, now=now) for d in pending]
                if conv.next_action is None and conv.status == "active" and (conv.slots or {}).get("expecting") not in (None, "booked"):
                    conv.next_action, conv.next_action_at = "stall_1", now + timedelta(hours=_hours(s.sms_stall_hours)[0])
                return "sent" if "sent" in results else (results[0] if results else "skipped")
            conv.next_action = None
            return "unknown_action"

    async def _reminders(self, now) -> dict:
        s = get_settings()
        offsets = sorted(_hours(s.sms_reminder_hours), reverse=True)
        if not offsets:
            return {}
        sent = 0
        async with session_scope() as db:
            rows = (await db.execute(
                select(Appointment.id).join(BookingSlot, BookingSlot.id == Appointment.slot_id).where(
                    Appointment.tenant_id == s.default_tenant_id,   # the tenant the gate checked
                    Appointment.conversation_id.is_not(None), Appointment.status == "scheduled",
                    BookingSlot.starts_at > now + timedelta(minutes=15),
                    BookingSlot.starts_at <= now + timedelta(hours=max(offsets)))
            )).scalars().all()
        for appt_id in rows:
            async with session_scope() as db:
                appt = await db.get(Appointment, appt_id)
                slot = await db.get(BookingSlot, appt.slot_id)
                conv = await db.get(SmsConversation, appt.conversation_id)
                lead = await db.get(Lead, appt.lead_id)
                done = list(appt.reminders_sent or [])
                for h in offsets:
                    key = f"{h:g}h"
                    if key in done or now < slot.starts_at - timedelta(hours=h):
                        continue
                    if not in_tcpa_window(lead.state, now):
                        break  # try again when the window opens
                    body = self._say(conv, lead, dict(conv.slots or {}), "reminder", key,
                                     when=playbook.when(slot.starts_at, lead.state))
                    result = await self._send(db, conv, lead, Outgoing("reminder", body, f"reminder:{appt.id}:{key}"),
                                              reply=False, now=now)
                    if result in ("sent", "duplicate"):
                        appt.reminders_sent = done + [h_ for h_ in [f"{x:g}h" for x in offsets] if
                                                       now >= slot.starts_at - timedelta(hours=float(h_[:-1]))]
                        sent += result == "sent"
                    break
        return {"reminders_sent": sent} if sent else {}
