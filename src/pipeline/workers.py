import logging
import threading
import time
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict

from ..core.clock import now_iso
from ..observability.metrics import events_in, events_out, dlq_total, stage_time
from .consumer import ConsumerLoop
from .dlq import to_dlq

log = logging.getLogger("eastwood.workers")

def start_workers(ctx) -> None:
    s = ctx.settings
    t = ctx.topics

    def publish(topic: str, msg: Dict[str, Any]) -> None:
        ctx.producer.send(topic, msg)
        events_out.labels(topic).inc()

    def send_dlq(topic: str, reason: str, payload: Dict[str, Any], error: str | None = None) -> None:
        dlq_total.labels(reason).inc()
        publish(t.dlq_events, to_dlq(topic=topic, reason=reason, payload=payload, error=error))

    def score_lead(lead: Dict[str, Any]) -> int:
        base = {"annuity": 90, "iul": 80, "final_expense": 60}
        product = str(lead.get("product") or "").lower()
        st = str(lead.get("state") or "").upper()
        payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
        score = int(base.get(product, 50))

        timeline = str(payload.get("timeline") or "").lower()
        if timeline in ("today", "0d", "now", "same_day"):
            score += 15
        elif "7" in timeline:
            score += 10
        elif "30" in timeline:
            score += 5

        try:
            target = int(payload.get("coverage_target") or 0)
        except Exception:
            target = 0
        if target >= 250000:
            score += 15
        if target >= 500000:
            score += 5

        if lead.get("email"):
            score += 5

        if st in ("CA", "NY", "NJ", "MA", "CT"):
            score += 5

        try:
            age = int(payload.get("age") or 0)
        except Exception:
            age = 0
        if age and 25 <= age <= 55:
            score += 5

        smoker = payload.get("smoker")
        if smoker is True:
            score -= 5

        if score < 0:
            score = 0
        if score > 100:
            score = 100
        return score

    def carrier_options_for(lead: Dict[str, Any]) -> Dict[str, Any]:
        product = str(lead.get("product") or "")
        state = str(lead.get("state") or "").upper()
        carriers = s.carriers or {}
        by_product = carriers.get(product) if isinstance(carriers, dict) else None
        by_state = (by_product or {}).get(state) if isinstance(by_product, dict) else None
        preferred = list((by_state or {}).get("preferred") or [])
        fallback = list((by_state or {}).get("fallback") or [])
        return {"preferred": preferred, "fallback": fallback}

    def handle_validated(lead: Dict[str, Any]) -> None:
        events_in.labels(t.leads_validated).inc()
        with stage_time.labels("score").time():
            try:
                sc = score_lead(lead)
                lead["score"] = int(sc)
                ctx.leads.set_score(lead["lead_id"], int(sc))
                ctx.leads.set_status(lead["lead_id"], "scored")
                publish(t.leads_scored, lead)
            except Exception as e:
                send_dlq(t.leads_validated, "score_exception", lead, str(e))

    def handle_scored(lead: Dict[str, Any]) -> None:
        events_in.labels(t.leads_scored).inc()
        with stage_time.labels("route").time():
            try:
                routed_to = "default_queue"
                st = (lead.get("state") or "").upper()
                if st in ("CA", "TX", "FL", "NY"):
                    routed_to = f"queue_{st.lower()}"

                opts = carrier_options_for(lead)
                lead["carrier_options"] = opts

                ctx.leads.set_routed(lead["lead_id"], routed_to)
                ctx.leads.merge_payload(lead["lead_id"], {"carrier_options": opts})
                lead["routed_to"] = routed_to
                ctx.leads.set_status(lead["lead_id"], "routed")
                publish(t.leads_routed, lead)
            except Exception as e:
                send_dlq(t.leads_scored, "route_exception", lead, str(e))

    def build_profile(lead: Dict[str, Any]) -> str:
        payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}
        product = str(lead.get("product") or "").upper()
        state = str(lead.get("state") or "")
        zipc = str(lead.get("zip") or "")
        score = str(lead.get("score") or "")
        age = payload.get("age")
        smoker = payload.get("smoker")
        target = payload.get("coverage_target")
        timeline = payload.get("timeline")

        opts = payload.get("carrier_options") if isinstance(payload.get("carrier_options"), dict) else lead.get("carrier_options") or {}
        pref = " > ".join([str(x) for x in (opts.get("preferred") or [])]) or "-"
        fb = " > ".join([str(x) for x in (opts.get("fallback") or [])]) or "-"

        opener = "You said 7 days. What happened that made this urgent today?"
        if str(timeline or "").lower() in ("today", "now", "0d", "same_day"):
            opener = "You said today. What made this a must-handle-right-now problem?"
        elif "30" in str(timeline or ""):
            opener = "You said 30 days. What are you trying to have solved by then?"

        lines = []
        lines.append(f"NEW {product} score {score} {state} {zipc}")
        lines.append(f"Phone {lead.get('phone_e164','')} Email {lead.get('email','') or '-'}")
        lines.append(f"Timeline {timeline or '-'} | Age {age or '-'} | Smoker {smoker if smoker is not None else '-'} | Target {target or '-'}")
        lines.append(f"Preferred {pref}")
        lines.append(f"Fallback {fb}")
        lines.append(f"Opener: {opener}")
        lines.append(f"lead_id={lead.get('lead_id','')}")
        return "\n".join(lines)

    def handle_routed(lead_msg: Dict[str, Any]) -> None:
        events_in.labels(t.leads_routed).inc()
        try:
            lead_id = str(lead_msg.get("lead_id") or "")
            if not lead_id:
                return

            key = f"touch:init:{lead_id}"
            if not ctx.redis.client.set(key, "1", nx=True, ex=7 * 86400):
                return

            lead = ctx.leads.get(lead_id) or {}
            if not lead:
                return

            if not ctx.consent.exists_for_lead(lead_id):
                return

            tok = ctx.booking.get_or_create(lead_id, ctx.settings.booking_token_ttl_hours)
            link = f"{ctx.settings.booking_public_base_url}/?token={tok['token']}#book"

            lead_body = f"Your link: {link} Pick a time. Reply STOP to opt out."
            agent_num = str(ctx.settings.agent_notify_number or "")
            profile = build_profile(lead)
            agent_body = f"{profile}\nLink: {link}"

            ctx.sms.send(to=str(lead.get("phone_e164") or ""), body=lead_body, lead_id=lead_id, kind="initial")
            ctx.sms.send(to=agent_num, body=agent_body, lead_id=lead_id, kind="agent_profile")
        except Exception:
            log.exception("notify failed")

    def reminder_loop() -> None:
        tz = ZoneInfo(str(ctx.settings.work_hours.get("tz") or "America/Detroit"))
        while True:
            try:
                now = datetime.now(timezone.utc)
                horizon = now + timedelta(hours=26)
                appts = ctx.appointments.list_booked_with_lead_between(now, horizon)
                for a in appts:
                    start_at = a["start_at"]
                    if not isinstance(start_at, datetime):
                        continue
                    delta = start_at - now
                    lead_id = a["lead_id"]
                    token = ctx.booking.get_active_for_lead(lead_id)
                    link = f"{ctx.settings.booking_public_base_url}/?token={(token or {}).get('token','')}#book"

                    local_str = start_at.astimezone(tz).strftime("%a %b %d %I:%M %p")
                    to_lead = str(a.get("phone_e164") or "")
                    to_agent = str(ctx.settings.agent_notify_number or "")

                    if a.get("reminder_24h_sent_at") is None and timedelta(hours=23, minutes=30) <= delta <= timedelta(hours=24, minutes=30):
                        msg = f"Reminder: {local_str} ({a.get('tz')}). Link: {link} Reply STOP to opt out."
                        ctx.sms.send(to=to_lead, body=msg, lead_id=lead_id, kind="reminder_24h")
                        ctx.sms.send(to=to_agent, body=f"REMINDER 24H lead_id={lead_id} {local_str} {a.get('tz')}", lead_id=lead_id, kind="agent_reminder_24h")
                        ctx.appointments.mark_reminder_24h_sent(a["appointment_id"])

                    if a.get("reminder_2h_sent_at") is None and timedelta(hours=1, minutes=45) <= delta <= timedelta(hours=2, minutes=15):
                        msg = f"Reminder: {local_str} ({a.get('tz')}) in ~2 hours. Link: {link} Reply STOP to opt out."
                        ctx.sms.send(to=to_lead, body=msg, lead_id=lead_id, kind="reminder_2h")
                        ctx.sms.send(to=to_agent, body=f"REMINDER 2H lead_id={lead_id} {local_str} {a.get('tz')}", lead_id=lead_id, kind="agent_reminder_2h")
                        ctx.appointments.mark_reminder_2h_sent(a["appointment_id"])
            except Exception:
                log.exception("reminder loop failed")
            time.sleep(45)

    def spawn(loop: ConsumerLoop) -> None:
        threading.Thread(target=loop.run, daemon=True).start()

    spawn(ConsumerLoop(ctx.settings.kafka_bootstrap, t.leads_validated, "eastwood-score", handle_validated, auto_offset_reset="earliest"))
    spawn(ConsumerLoop(ctx.settings.kafka_bootstrap, t.leads_scored, "eastwood-route", handle_scored, auto_offset_reset="earliest"))
    spawn(ConsumerLoop(ctx.settings.kafka_bootstrap, t.leads_routed, "eastwood-notify", handle_routed, auto_offset_reset="earliest"))
    threading.Thread(target=reminder_loop, daemon=True).start()