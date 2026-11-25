"""SMS-first engine against a real database (EASTWOOD_INTEGRATION=1).

The commercial acceptance path at engine level: outreach → conversation →
quote from a verified rate sheet → booking → confirmation → reminders →
reschedule / cancel, plus objections, incomplete answers, unquotable cases,
follow-ups, quiet hours, declines and opt-outs. A fake phone records every
text; a controlled clock moves time. Each test runs in its own tenant.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, text

pytestmark = pytest.mark.skipif(os.environ.get("EASTWOOD_INTEGRATION") != "1",
                                reason="set EASTWOOD_INTEGRATION=1 with a real database")

AGENCY = "Eastwood Test Agency"
TUESDAY_3PM_ET = datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc)


class FakePhone:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, to, body):
        self.sent.append((to, body))
        return f"SMfake{uuid.uuid4().hex[:20]}"


class World:
    pass


@pytest.fixture
async def world(monkeypatch, tmp_path):
    import config.settings as cs
    import db.session as dbs
    from db.models import Campaign, Lead, OutboundControlEvent
    from domain.quoting.rate_sheets import import_rate_sheet, verify_rate_sheet
    from domain.sms.engine import SmsEngine
    from tests.fixtures.rate_sheets import make_test_sheet

    w = World()
    w.tenant = f"it-{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={
        "default_tenant_id": w.tenant, "consent_authorized_sellers": AGENCY, "agency_display_name": AGENCY,
        "agent_display_name": "Ryan", "outbound_enabled": True, "allow_test_rate_sheets": True,
        "prospect_quotes_require_verified": True, "agent_availability": "mon-sun 08:00-20:00",
        "appointment_max_per_day": 6, "appointment_min_notice_hours": 2, "appointment_horizon_days": 14,
        "calendly_default_timezone": "America/Detroit", "agent_notification_phone_e164": "",
        "sms_followup_hours": "48,120", "sms_stall_hours": "24,72", "sms_reminder_hours": "24,2",
        "sms_reply_outside_hours": False, "sms_messages_per_minute": 600, "outbound_daily_spend_limit_usd": 100.0,
        "pause_dialing_when_calendar_full": True, "sms_llm_provider": "none", "dnc_test_allowlist": "",
        "active_states": "MI,OH,WI",
    }))
    monkeypatch.delenv("BYPASS_TCPA", raising=False)
    dbs._engine, dbs._SessionLocal = None, None

    make_test_sheet.write(tmp_path)
    import csv
    import json
    async with dbs.session_scope() as db:
        sheet = await import_rate_sheet(db, tmp_path / "rates.csv", json.loads((tmp_path / "meta.json").read_text()), w.tenant)
        res = await verify_rate_sheet(db, sheet.id, checks=list(csv.DictReader(open(tmp_path / "checks.csv"))),
                                      verified_by="integration test", tenant_id=w.tenant)
        assert res["passed"], res
        w.sheet_id = sheet.id
        c = Campaign(tenant_id=w.tenant, name="SMS IT", status="active", budget_usd=50, channel_strategy="sms_first",
                     messages_per_minute=60, states=["MI", "OH", "WI"], product_lines=["final_expense"])
        db.add(c)
        db.add(OutboundControlEvent(tenant_id=w.tenant, action="resume", operator="it", reason="integration"))
        await db.flush()
        w.campaign_id = c.id

    async def add_lead(n: int, *, state="MI", first="Pat", age=None, sms=True):
        from domain.campaigns import enqueue_sms, get_campaign

        phone = f"+1313555{100 + n:04d}"
        async with dbs.session_scope() as db:
            db.add(Lead(tenant_id=w.tenant, phone_e164=phone, first_name=first, last_name="Synthetic", state=state,
                        product_line="final_expense", status="qualified", national_dnc_status="clear", age=age,
                        contact_authorization={"ai_voice": True, "sms": sms}, quarantine_reasons=[]))
            await db.flush()
            await enqueue_sms(db, await get_campaign(db, w.campaign_id, w.tenant))
        return phone

    w.add_lead = add_lead
    w.phone = FakePhone()
    w.now = TUESDAY_3PM_ET
    w.engine = SmsEngine(w.phone, clock=lambda: w.now)

    async def say(phone, body):
        before = len(w.phone.sent)
        await w.engine.handle_inbound(phone=phone, body=body, provider_sid=f"SMin{uuid.uuid4().hex[:20]}")
        return [b for _, b in w.phone.sent[before:]]

    async def tick():
        before = len(w.phone.sent)
        await w.engine.run_due()
        return [b for _, b in w.phone.sent[before:]]

    w.say, w.tick, w.db = say, tick, dbs
    yield w

    async with dbs.session_scope() as db:
        p = {"t": w.tenant}
        for sql in (
            "DELETE FROM sms_messages WHERE conversation_id IN (SELECT id FROM sms_conversations WHERE tenant_id = :t)",
            "DELETE FROM sms_conversations WHERE tenant_id = :t",
            "DELETE FROM handoff_profiles WHERE tenant_id = :t",
            "DELETE FROM appointments WHERE tenant_id = :t",
            "DELETE FROM booking_slots WHERE tenant_id = :t",
            "DELETE FROM quote_records WHERE lead_id IN (SELECT id FROM leads WHERE tenant_id = :t)",
            "DELETE FROM spend_ledger WHERE tenant_id = :t",
            "DELETE FROM dial_queue WHERE tenant_id = :t",
            "DELETE FROM outbound_control_events WHERE tenant_id = :t",
            "DELETE FROM campaigns WHERE tenant_id = :t",
            "DELETE FROM rate_sheet_rates WHERE sheet_id IN (SELECT id FROM rate_sheets WHERE tenant_id = :t)",
            "DELETE FROM rate_sheets WHERE tenant_id = :t",
            "DELETE FROM suppressions WHERE tenant_id = :t",
            "DELETE FROM leads WHERE tenant_id = :t",
        ):
            await db.execute(text(sql), p)
    await dbs._engine.dispose()
    dbs._engine, dbs._SessionLocal = None, None


async def _rows(w, sql, **p):
    async with w.db.session_scope() as db:
        return (await db.execute(text(sql), {"t": w.tenant, **p})).all()


def _amounts(body: str) -> list[Decimal]:
    return [Decimal(a.replace(",", "")) for a in re.findall(r"\$(\d[\d,]*(?:\.\d{2})?)", body)]


async def _qualify(w, phone, *, face="15000", age="I'm 67", tob="no", sex="female", health="no", budget="no"):
    assert "coverage" in (await w.say(phone, "Yes please"))[-1].lower()
    assert "old" in (await w.say(phone, face))[-1].lower() or "age" in w.phone.sent[-1][1].lower()
    assert "tobacco" in (await w.say(phone, age))[-1].lower()
    assert "male or female" in (await w.say(phone, tob))[-1].lower()
    assert "(yes or no)" in (await w.say(phone, sex))[-1].lower()
    assert "stay under" in (await w.say(phone, health))[-1].lower()
    return await w.say(phone, budget)


async def test_upload_to_booked_appointment_with_exact_quote(world):
    from tests.fixtures.rate_sheets.make_test_sheet import monthly

    w = world
    phone = await w.add_lead(1)
    opener = await w.tick()
    assert len(opener) == 1 and AGENCY in opener[0] and "AI assistant" in opener[0] and "STOP" in opener[0]

    quote_text = (await _qualify(w, phone))[-1]
    assert "TEST RATES" in quote_text and "Reply 1, 2 or 3" in quote_text
    # every dollar figure in the text is a stored quote, computed exactly from the sheet
    quotes = await _rows(w, "SELECT q.face_value, q.monthly_premium, q.quote_ref, q.source, q.rate_sheet_id, q.communicated_at "
                            "FROM quote_records q JOIN leads l ON l.id = q.lead_id WHERE l.tenant_id = :t ORDER BY q.face_value")
    assert len(quotes) == 3
    allowed = set()
    for face_cents, premium, ref, source, sheet, communicated in quotes:
        face = int(face_cents / 100)
        assert premium == monthly(67, "F", False, "level", face), "premium must equal the independent computation"
        assert ref.startswith("Q") and source == "rate_sheet_test" and sheet == w.sheet_id and communicated is not None
        allowed |= {Decimal(face), premium}
    assert set(_amounts(quote_text)) == allowed

    times = (await w.say(phone, "2"))[-1]
    assert times.count(" ET") == 3 and "Reply 1, 2 or 3" in times
    booked = (await w.say(phone, "1"))[-1]
    assert "You're booked" in booked and "ET" in booked

    appt = (await _rows(w, "SELECT a.id, a.status, s.starts_at, s.available FROM appointments a "
                           "JOIN booking_slots s ON s.id = a.slot_id WHERE a.tenant_id = :t"))[0]
    assert appt[1] == "scheduled" and appt[3] is False
    profile = (await _rows(w, "SELECT profile FROM handoff_profiles WHERE tenant_id = :t"))[0][0]
    for key in ("full_name", "phone_e164", "appointment_time", "desired_coverage_amount", "health_summary",
                "quote_options", "selected_option", "quoted_monthly_premium", "carrier_name", "quote_ref",
                "transcript_summary", "recommended_next_action", "unanswered_questions", "tobacco_status"):
        assert key in profile and profile[key] not in (None, ""), key if key != "unanswered_questions" else None
    assert profile["selected_option"] == "2" and len(profile["quote_options"]) == 3
    assert Decimal(profile["quoted_monthly_premium"]) == monthly(67, "F", False, "level", 20000)
    assert "Prospect: 15000" in profile["transcript_summary"] and profile["quote_ref"] in profile["recommended_next_action"]

    # reminders: 24 h and 2 h before, each once
    start = appt[2]
    w.now = start - timedelta(hours=23)
    assert any(b.startswith("Reminder:") for b in await w.tick())
    assert not any(b.startswith("Reminder:") for b in await w.tick())
    w.now = start - timedelta(hours=1, minutes=50)
    assert any(b.startswith("Reminder:") for b in await w.tick())

    # reschedule releases the slot and books a new one; cancel releases again
    w.now = start - timedelta(hours=1, minutes=40)
    w.now = TUESDAY_3PM_ET + timedelta(minutes=30)
    assert "other times" in (await w.say(phone, "Can I reschedule?"))[-1]
    old = (await _rows(w, "SELECT a.status, a.outcome, s.available FROM appointments a JOIN booking_slots s ON s.id = a.slot_id "
                          "WHERE a.id = :a", a=appt[0]))[0]
    assert tuple(old) == ("cancelled", "rescheduled", True)
    assert "You're booked" in (await w.say(phone, "2"))[-1]
    assert "cancelled" in (await w.say(phone, "C"))[-1]
    statuses = [r[0] for r in await _rows(w, "SELECT status FROM appointments WHERE tenant_id = :t")]
    assert statuses.count("cancelled") == 2


async def test_objections_incomplete_answers_and_questions(world):
    w = world
    phone = await w.add_lead(2, first="Lee")
    await w.tick()
    assert "Imogen" in (await w.say(phone, "Who is this?"))[-1]  # answered, asked again
    assert "coverage" in (await w.say(phone, "ok yes"))[-1].lower()
    assert "age" in (await w.say(phone, "not sure, whatever is normal"))[-1].lower() or "old" in w.phone.sent[-1][1]
    for reply in ("I'm 70", "no", "male", "no", "$60 a month"):
        last = await w.say(phone, reply)
    quote_text = last[-1]
    low = min(_amounts(quote_text)[1::2])  # premiums are every second amount
    price = (await w.say(phone, "That's too expensive"))[-1]
    assert "lowest option" in price and low in _amounts(price)
    assert "one of these times" in (await w.say(phone, "Yes that works"))[-1] or "Pick a time" in w.phone.sent[-1][1]

    phone2 = await w.add_lead(3, first="Sam")
    await w.tick()
    await _qualify(w, phone2)
    assert "spouse" in (await w.say(phone2, "I need to talk to my wife first"))[-1]
    assert "Reply 1, 2 or 3" in (await w.say(phone2, "Yes"))[-1]


async def test_unquotable_case_still_books_without_a_number(world):
    w = world
    phone = await w.add_lead(4)
    await w.tick()
    last = await _qualify(w, phone, age="I'm 89")
    assert "can't give you an exact rate" in last[-1] and not _amounts(last[-1])
    assert "one of these times" in (await w.say(phone, "Yes"))[-1] or "Pick a time" in w.phone.sent[-1][1]
    assert "You're booked" in (await w.say(phone, "1"))[-1]
    profile = (await _rows(w, "SELECT profile FROM handoff_profiles WHERE tenant_id = :t"))[0][0]
    assert "No exact rate" in profile["recommended_next_action"] and profile["quoted_monthly_premium"] is None


async def test_followups_dormancy_interruption_and_quiet_hours(world):
    w = world
    silent = await w.add_lead(5)
    assert len(await w.tick()) == 1
    w.now += timedelta(hours=48, minutes=1)
    assert any("checking in" in b or "again" in b for b in await w.tick())
    w.now += timedelta(hours=72, minutes=1)
    assert any("Last note" in b for b in await w.tick())
    w.now += timedelta(days=5)
    assert await w.tick() == []
    assert (await _rows(w, "SELECT status FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p", p=silent))[0][0] == "dormant"

    w.now = TUESDAY_3PM_ET
    talker = await w.add_lead(6)
    await w.tick()
    await w.say(talker, "Yes please")
    w.now += timedelta(hours=24, minutes=1)
    nudge = await w.tick()
    assert any("coverage" in b.lower() for b in nudge)
    back = (await w.say(talker, "15000"))[-1]
    assert "Welcome back" in back or "Good to hear" in back

    # 11:30 PM Eastern: no outreach until 8 AM
    w.now = datetime(2026, 12, 2, 4, 30, tzinfo=timezone.utc)
    await w.add_lead(7)
    assert await w.tick() == []
    w.now = datetime(2026, 12, 2, 13, 5, tzinfo=timezone.utc)  # 8:05 AM Eastern
    assert len(await w.tick()) >= 1


async def test_decline_wrong_number_stop_and_duplicates(world):
    from domain.compliance.sms_opt_out import apply_opt_out

    w = world
    a, b, c = await w.add_lead(8), await w.add_lead(9), await w.add_lead(10)
    await w.tick()
    assert "won't text you" in (await w.say(a, "No thanks, not interested."))[-1]
    assert await w.say(a, "Actually what are the rates?") == []
    assert "remove this number" in (await w.say(b, "wrong number"))[-1]
    reasons = {r[0] for r in await _rows(w, "SELECT reason FROM suppressions WHERE tenant_id = :t")}
    assert {"sms_declined", "wrong_number"} <= reasons
    async with w.db.session_scope() as db:
        await apply_opt_out(db, w.tenant, c, note="test STOP")
    w.now += timedelta(hours=49)
    assert await w.tick() == []

    d = await w.add_lead(11)
    await w.tick()
    sid = "SMdup" + uuid.uuid4().hex[:20]
    first = await w.engine.handle_inbound(phone=d, body="Yes please", provider_sid=sid)
    second = await w.engine.handle_inbound(phone=d, body="Yes please", provider_sid=sid)
    assert first["handled"] and second == {"handled": False, "reason": "duplicate"}


async def test_policy_blocks_any_amount_not_from_a_stored_quote(world):
    from db.models import Lead, SmsConversation
    from domain.sms.engine import Outgoing

    w = world
    phone = await w.add_lead(12)
    await w.tick()
    async with w.db.session_scope() as db:
        conv = (await db.execute(select(SmsConversation).where(SmsConversation.phone_e164 == phone))).scalars().one()
        lead = await db.get(Lead, conv.lead_id)
        result = await w.engine._send(db, conv, lead, Outgoing("x", "Your rate is $49.99/mo", "x:1"), reply=True, now=w.now)
    assert result == "policy_blocked"


async def test_calendar_full_pauses_new_outreach(world, monkeypatch):
    import config.settings as cs

    w = world
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={"agent_availability": "sun 08:00-08:30",
                                                                              "appointment_horizon_days": 1}))
    await w.add_lead(13)
    assert await w.tick() == []


async def _offered(w, phone):
    return (await _rows(w, "SELECT slots->'offered' FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p",
                        p=phone))[0][0]


async def test_simultaneous_choosers_are_offered_different_times(world):
    w = world
    a, b = await w.add_lead(20, first="Ann"), await w.add_lead(21, first="Bo")
    await w.tick()
    for p in (a, b):
        await _qualify(w, p)
    await w.say(a, "1")
    await w.say(b, "1")
    oa, ob = await _offered(w, a), await _offered(w, b)
    assert len(oa) == 3 and len(ob) == 3 and not set(oa) & set(ob)
    # spread out, not three back-to-back slots
    starts = [datetime.fromisoformat(t) for t in oa]
    assert all((y - x) >= timedelta(minutes=90) for x, y in zip(starts, starts[1:]))


async def test_three_taken_picks_in_a_row_hand_off_to_the_agent(world):
    from config.settings import get_settings
    from domain.scheduling.local import LocalCalendar

    w = world
    phone = await w.add_lead(22)
    await w.tick()
    await _qualify(w, phone)
    await w.say(phone, "1")
    cal = LocalCalendar.from_settings(get_settings())
    for attempt in range(3):
        first = (await _offered(w, phone))[0]
        async with w.db.session_scope() as db:              # someone else takes the time first
            await cal.reserve(db, w.tenant, datetime.fromisoformat(first), now=w.now)
        reply = (await w.say(phone, "1"))[-1]
        if attempt < 2:
            assert "just taken" in reply
    assert "know you'd like a call" in reply
    assert (await _rows(w, "SELECT status, next_action FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p", p=phone))[0] == \
        ("handoff", None)
    later = await w.say(phone, "2")                       # no further automated time offers
    assert not any(" ET" in b for b in later), later


async def test_conversation_cap_stops_a_runaway_conversation(world, monkeypatch):
    import config.settings as cs

    w = world
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={"sms_max_outbound_per_conversation": 4}))
    phone = await w.add_lead(23)
    assert len(await w.tick()) == 1                      # 1 opener
    for reply in ("Yes please", "15000", "I'm 67"):      # 3 replies -> 4 texts
        assert len(await w.say(phone, reply)) == 1
    assert await w.say(phone, "no") == []                # the 5th is never sent
    row = (await _rows(w, "SELECT status, outbound_count FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p", p=phone))[0]
    assert tuple(row) == ("handoff", 4)


async def test_two_options_are_offered_as_two_and_repeated_misses_hand_off(world):
    w = world
    phone = await w.add_lead(24)
    await w.tick()
    quote_text = (await _qualify(w, phone, face="35000"))[-1]     # 45,000 is over the sheet's maximum
    assert "Reply 1 or 2" in quote_text and "1, 2 or 3" not in quote_text
    assert "Reply 1 or 2 to pick" in (await w.say(phone, "3"))[-1]
    assert "Reply 1 or 2 to pick" in (await w.say(phone, "3"))[-1]
    assert "know you'd like a call" in (await w.say(phone, "3"))[-1]   # third miss: the agent takes over
    row = (await _rows(w, "SELECT status, next_action FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p", p=phone))[0]
    assert tuple(row) == ("handoff", None)


async def test_a_reply_and_the_text_it_answers_keep_their_order(world):
    w = world
    phone = await w.add_lead(25)
    await w.tick()
    await w.say(phone, "Yes please")
    rows = await _rows(w, "SELECT m.direction FROM sms_messages m JOIN sms_conversations c ON c.id = m.conversation_id "
                          "WHERE c.tenant_id = :t AND c.phone_e164 = :p ORDER BY m.created_at", p=phone)
    assert [r[0] for r in rows] == ["out", "in", "out"]


async def test_stop_is_recorded_in_the_transcript_once(world):
    from domain.compliance.sms_opt_out import apply_opt_out

    w = world
    phone = await w.add_lead(26)
    await w.tick()
    for _ in range(2):                                   # Twilio retries the webhook: still one row
        async with w.db.session_scope() as db:
            await apply_opt_out(db, w.tenant, phone, note="test", body="STOP", provider_sid="SMstop0001")
    rows = await _rows(w, "SELECT m.direction, m.body, m.intent FROM sms_messages m JOIN sms_conversations c "
                          "ON c.id = m.conversation_id WHERE c.tenant_id = :t AND c.phone_e164 = :p ORDER BY m.created_at", p=phone)
    assert [tuple(r) for r in rows][-1] == ("in", "STOP", "opt_out") and len(rows) == 2
    conv = (await _rows(w, "SELECT status, inbound_count FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p", p=phone))[0]
    assert tuple(conv) == ("opted_out", 1)
    assert await w.tick() == []                          # and nothing more is ever sent


async def test_waitlist_texts_only_when_a_time_opens(world, monkeypatch):
    import config.settings as cs
    from config.settings import get_settings
    from domain.scheduling.local import LocalCalendar

    w = world
    # a one-slot-a-day calendar on Tuesday afternoons only
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={
        "agent_availability": "tue 16:00-16:30", "appointment_horizon_days": 7, "appointment_slot_minutes": 30}))
    phone = await w.add_lead(27)
    await w.tick()
    await _qualify(w, phone)
    offer = (await w.say(phone, "1"))[-1]
    assert "Reply 1" in offer and len(await _offered(w, phone)) == 1
    cal = LocalCalendar.from_settings(get_settings())
    async with w.db.session_scope() as db:                    # someone else books the only time
        await cal.reserve(db, w.tenant, datetime.fromisoformat((await _offered(w, phone))[0]), now=w.now)
    full = (await w.say(phone, "1"))[-1]
    assert "calendar is full" in full
    row = (await _rows(w, "SELECT status, next_action, slots ? 'offered' FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p",
                       p=phone))[0]
    assert tuple(row) == ("waitlist", "retry_booking", False)

    w.now += timedelta(hours=24, minutes=1)                    # still full: re-checks, texts nothing
    assert await w.tick() == []
    assert (await _rows(w, "SELECT status FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p", p=phone))[0][0] == "waitlist"

    async with w.db.session_scope() as db:                    # the other prospect cancels
        await db.execute(text("UPDATE booking_slots SET available = true WHERE tenant_id = :t"), {"t": w.tenant})
    w.now += timedelta(hours=24, minutes=1)
    texts = await w.tick()
    assert len(texts) == 1 and " ET" in texts[0] and "Reply 1" in texts[0]


async def test_held_outreach_never_starves_follow_ups(world, monkeypatch):
    import config.settings as cs

    w = world
    await w.add_lead(28)                                             # a lead who never replies
    assert len(await w.tick()) == 1                                  # opener while the calendar has room
    # the calendar fills (no availability), and 120 more leads wait for outreach
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={"agent_availability": "sun 08:00-08:30",
                                                                              "appointment_horizon_days": 1}))
    for n in range(30, 150):
        await w.add_lead(n)
    w.now += timedelta(hours=48, minutes=1)
    sent = await w.tick()
    assert len(sent) == 1 and "rates" in sent[0]                     # the follow-up still goes out
    rows = await _rows(w, "SELECT count(*) FROM sms_conversations WHERE tenant_id = :t AND outbound_count = 0")
    assert rows[0][0] == 120                                         # and no held lead was texted


async def test_a_rate_limited_booking_confirmation_still_goes_out(world, monkeypatch):
    import config.settings as cs

    w = world
    phone = await w.add_lead(29)
    await w.tick()
    await _qualify(w, phone)
    await w.say(phone, "1")                                        # times offered
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={"sms_messages_per_minute": 1}))
    assert await w.say(phone, "1") == []                           # booked, but the confirmation is rate-limited
    row = (await _rows(w, "SELECT status, next_action FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p",
                       p=phone))[0]
    assert tuple(row) == ("booked", "deferred")
    monkeypatch.setattr(cs, "_settings", cs.get_settings().model_copy(update={"sms_messages_per_minute": 600}))
    w.now += timedelta(seconds=21)
    assert any("You're booked" in b for b in await w.tick())       # delivered on the next pass
    row = (await _rows(w, "SELECT status, next_action FROM sms_conversations WHERE tenant_id = :t AND phone_e164 = :p",
                       p=phone))[0]
    assert tuple(row) == ("booked", None)
