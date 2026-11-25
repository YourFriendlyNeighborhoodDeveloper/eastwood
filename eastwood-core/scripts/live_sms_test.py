#!/usr/bin/env python3
"""Controlled LIVE SMS test: one real conversation with ONE authorized phone.

PREPARED, NOT RUN. Read docs/controlled-live-sms-test.md first. It sends real
texts through Twilio, so it refuses to start unless every guard below holds:

  * real provider: TELEPHONY_PROVIDER=twilio, Twilio credentials, an https
    PUBLIC_BASE_URL; no simulated prospects
  * a dedicated tenant: DEFAULT_TENANT_ID starts with "live-test", and that
    tenant holds no lead except the test recipient and no other active campaign
  * the recipient is named twice: --to and the LIVE_TEST_RECIPIENT environment
    variable must match, and you type its last four digits to confirm
  * hard caps: a $5 campaign budget, 25 texts a day, 5 texts a minute

The lead goes through the real import pipeline (consent, DNC scrub,
qualification), so the test exercises the same gates as a real campaign. When
the conversation ends (booked, declined, opted out) or the time limit passes,
outbound is HALTED and the campaign completed, whatever happened.

  python -m scripts.live_sms_test --to +1XXXXXXXXXX --first-name Pat --last-name Example \\
      --state MI --dob 1958-04-02 --operator "Your Name" --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BUDGET_USD = 5.0
DAILY_MESSAGE_CAP = 25
MESSAGES_PER_MINUTE = 5


def preflight(s, *, to: str, env_recipient: str, tenant_phones: set[str], other_active_campaigns: int) -> list[str]:
    """Every reason not to start. Empty list = all guards hold."""
    problems = []
    if s.telephony_provider.strip().lower() != "twilio":
        problems.append("TELEPHONY_PROVIDER must be twilio (this is the live test)")
    if not (s.twilio_account_sid and s.twilio_auth_token and s.twilio_from_number):
        problems.append("Twilio credentials (rotated) and TWILIO_FROM_NUMBER are required")
    if not s.public_base_url.startswith("https://"):
        problems.append("PUBLIC_BASE_URL must be the https URL Twilio posts replies to")
    if getattr(s, "simulated_sms_prospects", False):
        problems.append("SIMULATED_SMS_PROSPECTS must be false")
    if not s.outbound_enabled:
        problems.append("OUTBOUND_ENABLED must be true (the arm switch) for the test window")
    if not s.default_tenant_id.startswith("live-test"):
        problems.append("DEFAULT_TENANT_ID must start with 'live-test' (a tenant used for nothing else)")
    if not to.startswith("+1") or len(to) != 12 or not to[1:].isdigit():
        problems.append("--to must be a +1 E.164 number, e.g. +13135550123")
    if to != env_recipient:
        problems.append("--to must equal the LIVE_TEST_RECIPIENT environment variable (named twice on purpose)")
    others = tenant_phones - {to}
    if others:
        problems.append(f"the live-test tenant holds {len(others)} other lead(s); it must hold only the recipient")
    if other_active_campaigns:
        problems.append(f"{other_active_campaigns} other campaign(s) are active in this tenant")
    return problems


def _write_lead_csv(folder: Path, a, agency: str) -> Path:
    consent = (a.consent_text or
               f"I, {a.first_name} {a.last_name}, agree that {agency} may contact me at {a.to} about insurance, "
               "including by text messages sent with automated technology and an AI assistant, for a controlled "
               "test of its texting service. I can reply STOP at any time.")
    p = folder / "live_test_lead.csv"
    with open(p, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["First Name", "Last Name", "Phone", "State", "DOB", "Gender", "Tobacco", "Product",
                    "TCPA Consent", "Consent Date", "Consent URL", "Lead Source"])
        w.writerow([a.first_name, a.last_name, a.to, a.state, a.dob, a.sex or "", a.tobacco or "", "Final Expense",
                    consent, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                    "operator-attested:controlled-live-test", "Controlled live test"])
    return p


async def _counts(session, tenant: str) -> tuple[set[str], int]:
    from sqlalchemy import text

    phones = {r[0] for r in (await session.execute(
        text("SELECT phone_e164 FROM leads WHERE tenant_id = :t"), {"t": tenant})).all()}
    active = (await session.execute(text(
        "SELECT count(*) FROM campaigns WHERE tenant_id = :t AND status = 'active'"), {"t": tenant})).scalar_one()
    return phones, int(active)


async def run(a) -> int:
    from sqlalchemy import select

    from config.settings import get_settings
    from db.models import SmsConversation, SmsMessage
    from db.session import session_scope

    s = get_settings()
    async with session_scope() as db:
        phones, active = await _counts(db, s.default_tenant_id)
    problems = preflight(s, to=a.to, env_recipient=os.environ.get("LIVE_TEST_RECIPIENT", ""),
                         tenant_phones=phones, other_active_campaigns=active)
    plan = {"recipient": a.to, "tenant": s.default_tenant_id, "agency": s.agency_display_name,
            "budget_usd": BUDGET_USD, "daily_message_cap": DAILY_MESSAGE_CAP, "messages_per_minute": MESSAGES_PER_MINUTE,
            "time_limit_minutes": a.minutes, "verified_rate_sheets_required": s.prospect_quotes_require_verified,
            "test_rate_sheets_allowed": s.allow_test_rate_sheets}
    print(json.dumps({"plan": plan, "refusals": problems}, indent=2))
    if problems:
        print("REFUSED: fix the items above. Nothing was sent.")
        return 2
    if a.dry_run:
        print("Dry run: guards hold. Nothing was created or sent.")
        return 0
    if not a.yes:
        typed = input(f"Type the last 4 digits of {a.to[:-4]}XXXX to send real texts to it: ").strip()
        if typed != a.to[-4:]:
            print("Not confirmed. Nothing was sent.")
            return 3

    from domain.campaigns import create_campaign, enqueue_sms, set_status
    from domain.imports import ImportOptions, run_import
    from domain.lead_ops import qualify_imported, scrub_unknown
    from domain.outbound import halt_outbound, resume_outbound

    campaign_id = None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_lead_csv(Path(tmp), a, s.agency_display_name)
            imp = await run_import(path, ImportOptions(
                source_name="Controlled live test", lead_owner=a.operator,
                use_authorization_ref=f"Controlled live test: recipient is {a.operator}'s own authorized number"))
        print("import:", json.dumps(imp.as_dict(), default=str))
        print("scrub:", json.dumps(await scrub_unknown(), default=str))
        print("qualify:", json.dumps(await qualify_imported(), default=str))
        async with session_scope() as db:
            camp = await create_campaign(db, {
                "name": f"Controlled live test {datetime.now(timezone.utc):%Y-%m-%d %H:%M}", "budget_usd": BUDGET_USD,
                "daily_budget_usd": BUDGET_USD, "states": [a.state], "product_lines": ["final_expense"],
                "channel_strategy": "sms_first", "messages_per_minute": MESSAGES_PER_MINUTE,
                "daily_message_cap": DAILY_MESSAGE_CAP}, s.default_tenant_id)
            campaign_id = camp.id
            enq = await enqueue_sms(db, camp, limit=1)
            print("enqueue:", json.dumps(enq, default=str))
            if enq["enqueued"] != 1:
                print("REFUSED: the recipient is not textable (see breakdown above). Nothing was sent.")
                return 4
            await set_status(db, camp, "active")
            await resume_outbound(db, operator=a.operator,
                                  reason=f"controlled live SMS test to {a.to[:-4]}XXXX (operator's own number)")
        print(f"Outbound authorized. Reply from {a.to[:-4]}XXXX as a prospect would. "
              f"Watching for {a.minutes} minutes; Ctrl-C halts.")
        seen, deadline = set(), asyncio.get_running_loop().time() + a.minutes * 60
        while asyncio.get_running_loop().time() < deadline:
            async with session_scope() as db:
                conv = (await db.execute(select(SmsConversation).where(
                    SmsConversation.campaign_id == campaign_id))).scalars().first()
                msgs = list((await db.execute(select(SmsMessage).where(
                    SmsMessage.conversation_id == conv.id).order_by(SmsMessage.created_at))).scalars()) if conv else []
            for m in msgs:
                if m.id not in seen:
                    seen.add(m.id)
                    who = "Imogen" if m.direction == "out" else "You"
                    print(f"[{m.created_at:%H:%M:%S}] {who} ({m.status}, {m.segments} seg): {m.body}")
            if conv and conv.status in ("booked", "declined", "opted_out", "closed", "handoff"):
                print(f"Conversation ended: {conv.status}")
                break
            await asyncio.sleep(5)
        return 0
    finally:
        async with session_scope() as db:
            await halt_outbound(db, operator=a.operator, reason="controlled live test finished")
            if campaign_id is not None:
                from db.models import Campaign

                camp = await db.get(Campaign, campaign_id)
                if camp is not None and camp.status == "active":
                    await set_status(db, camp, "completed", "controlled live test finished")
        print("Outbound HALTED and the test campaign completed. Review: /ops -> Conversations and Appointments.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="live_sms_test", description=__doc__.split("\n\n")[0])
    ap.add_argument("--to", required=True, help="the ONE authorized test phone, +1 E.164")
    ap.add_argument("--first-name", required=True)
    ap.add_argument("--last-name", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--dob", required=True, help="YYYY-MM-DD (quotes need an age)")
    ap.add_argument("--sex", choices=["M", "F"])
    ap.add_argument("--tobacco", choices=["Yes", "No"])
    ap.add_argument("--operator", required=True, help="who authorizes and watches the test")
    ap.add_argument("--consent-text", help="the recipient's own written consent (default: a test consent naming the agency)")
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="check the guards only")
    ap.add_argument("--yes", action="store_true", help="skip the typed confirmation (guards still apply)")
    a = ap.parse_args(argv)
    a.state = a.state.strip().upper()
    return asyncio.run(run(a))


if __name__ == "__main__":
    raise SystemExit(main())
