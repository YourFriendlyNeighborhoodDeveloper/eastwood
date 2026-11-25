"""Simulated telephone provider — load and end-to-end testing ONLY.

Drop-in for clients.twilio_sms.SmsClient (send / make_call). Nothing leaves
this stack: a "call" is a coroutine that, after SIMULATED_CALL_SECONDS of wall
time, delivers a Twilio-style status callback — signed with TWILIO_AUTH_TOKEN
over PUBLIC_BASE_URL exactly as Twilio signs — to SIMULATED_CALLBACK_BASE_URL
(nginx by default), so the real webhook path, signature validation, retry
ladder and spend recording all run.

Simulated prospects (seeded, so runs are repeatable), per call:
  ~50% no-answer, ~7% busy, ~3% failed, ~40% answered; of answered, ~35% reach
  voicemail (20–40 s) and ~65% a person (1.5–12 min). Of people: ~8% ask not
  to be called (dnc_request), ~45% hear a quote, and ~35% of those book an
  appointment against real availability. Those events go through Kafka and
  the real API exactly as ai-agent would send them.

Production refuses to boot with TELEPHONY_PROVIDER=simulated.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from urllib.parse import urlparse

import httpx
from prometheus_client import Counter
from twilio.request_validator import RequestValidator

logger = logging.getLogger(__name__)

simulated_calls_total = Counter(
    "eastwood_simulated_calls_total", "Calls completed by the simulated provider", ["outcome"]
)
simulated_sms_total = Counter("eastwood_simulated_sms_total", "SMS accepted by the simulated provider")


class SimulatedTelephony:
    simulated = True

    def __init__(self, settings, seed: int = 20261124) -> None:
        self._public_base = settings.public_base_url.rstrip("/")
        self._callback_base = settings.simulated_callback_base_url.rstrip("/")
        self._validator = RequestValidator(settings.twilio_auth_token or "simulated")
        self._call_seconds = max(float(settings.simulated_call_seconds), 0.05)
        self._api_key = settings.eastwood_api_key
        self._tz = settings.calendly_default_timezone
        self._rng = random.Random(seed)
        self._from_number = "+15005550006"
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        self.calls_started = 0
        self.sms_sent = 0
        self._calls: dict[str, dict] = {}

    # ── SMS ───────────────────────────────────────────────────────────────
    def send(self, to: str, body: str) -> str:
        self.sms_sent += 1
        simulated_sms_total.inc()
        return f"SMsim{uuid.uuid4().hex[:24]}"

    # ── Calls ─────────────────────────────────────────────────────────────
    def make_call(self, to: str, twiml_url: str, status_callback_url: str | None = None,
                  time_limit: int | None = None) -> str:
        sid = f"CAsim{uuid.uuid4().hex[:24]}"
        self.calls_started += 1
        plan = self._plan()
        if time_limit and plan.get("duration", 0) > time_limit:
            plan["duration"] = int(time_limit)  # the provider cuts the call, as Twilio's TimeLimit does
        plan["delay"] = self._call_seconds * (0.5 + self._rng.random())
        self._record(sid, plan)
        coro = self._complete(sid, twiml_url, status_callback_url, plan)
        if self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        else:  # called on the loop thread directly (tests)
            asyncio.get_event_loop().create_task(coro)
        return sid

    make_call_with_amd = make_call

    # ── Provider-side call records (like Twilio's Calls API) ──────────────
    # Persisted to a file so they survive a core restart, exactly as Twilio's
    # records survive ours — that is what lets the reconciler be tested.
    _LEDGER = "/tmp/eastwood_simulated_calls.jsonl"

    def _record(self, sid: str, plan: dict) -> None:
        import json
        import time

        rec = {"sid": sid, "status": plan["status"], "duration": plan.get("duration", 0),
               "answered_by": plan.get("answered_by"), "due_at": time.time() + plan["delay"]}
        self._calls[sid] = rec
        try:
            with open(self._LEDGER, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            logger.debug("simulated call ledger not writable", exc_info=True)

    def fetch_call(self, call_sid: str) -> dict:
        import json
        import time

        rec = self._calls.get(call_sid)
        if rec is None:
            try:
                with open(self._LEDGER, encoding="utf-8") as f:
                    for line in f:
                        r = json.loads(line)
                        self._calls[r["sid"]] = r
            except OSError:
                pass
            rec = self._calls.get(call_sid)
        if rec is None:
            raise LookupError(f"unknown simulated call {call_sid}")
        if time.time() < rec["due_at"]:
            return {"status": "in-progress", "duration": None, "answered_by": None}
        return {"status": rec["status"], "duration": rec["duration"], "answered_by": rec["answered_by"]}

    def _plan(self) -> dict:
        r = self._rng.random()
        if r < 0.50:
            return {"status": "no-answer", "duration": 0}
        if r < 0.57:
            return {"status": "busy", "duration": 0}
        if r < 0.60:
            return {"status": "failed", "duration": 0}
        if self._rng.random() < 0.35:
            return {"status": "completed", "answered_by": "machine_end_beep",
                    "duration": self._rng.randint(20, 40), "behaviour": "voicemail"}
        duration = self._rng.randint(90, 720)
        b = self._rng.random()
        if b < 0.08:
            behaviour = "dnc"
        elif b < 0.08 + 0.45:
            behaviour = "book" if self._rng.random() < 0.35 else "quoted"
        else:
            behaviour = "not_interested"
        return {"status": "completed", "answered_by": "human", "duration": duration, "behaviour": behaviour}

    async def _complete(self, sid: str, twiml_url: str, status_callback_url: str | None, plan: dict) -> None:
        try:
            await asyncio.sleep(plan["delay"])
            session_id = twiml_url.rstrip("/").rsplit("/", 1)[-1]
            if plan["status"] == "completed" and plan.get("answered_by") == "human":
                await self._prospect(session_id, plan)
            if status_callback_url:
                await self._status_callback(status_callback_url, sid, plan)
            simulated_calls_total.labels(outcome=plan.get("behaviour") or plan["status"]).inc()
        except Exception:
            logger.exception("simulated call %s failed to complete", sid)

    async def _status_callback(self, url: str, sid: str, plan: dict) -> None:
        params = {"CallSid": sid, "CallStatus": plan["status"], "CallDuration": str(plan.get("duration", 0))}
        if plan.get("answered_by"):
            params["AnsweredBy"] = plan["answered_by"]
        path = urlparse(url).path
        signature = self._validator.compute_signature(self._public_base + path, params)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(self._callback_base + path, data=params,
                                  headers={"X-Twilio-Signature": signature})
            if r.status_code != 200:
                logger.warning("simulated status callback %s → %s", path, r.status_code)

    async def _prospect(self, session_id: str, plan: dict) -> None:
        """Emit what ai-agent would emit for this simulated conversation."""
        from sqlalchemy import text

        from config.settings import get_settings
        from db.session import session_scope
        from kafka_io.kafka_client import publish

        s = get_settings()
        async with session_scope() as db:
            row = (await db.execute(text("SELECT lead_id FROM dial_queue WHERE session_id = :s"),
                                    {"s": session_id})).first()
        if row is None:
            return
        lead_id = str(row[0])
        behaviour = plan["behaviour"]
        outcome = {"dnc": "dnc", "quoted": "price_objection", "not_interested": "not_interested"}.get(behaviour)

        if behaviour == "dnc":
            await publish(s.topic_calls_dnc_requested,
                          {"eventType": "dnc_request", "leadId": lead_id, "sessionId": session_id}, key=lead_id)
        if behaviour in ("quoted", "book"):
            quote = {"label": "B", "face_value": 15000, "monthly_premium": 61.0, "quote_type": "estimated",
                     "rate_source": "formula_adapter", "carrier": None, "bindable": False}
            await publish(s.topic_calls_quote_presented,
                          {"eventType": "quote_presented", "leadId": lead_id, "sessionId": session_id,
                           "quote": quote}, key=lead_id)
        if behaviour == "book":
            booked = await self._book(lead_id, session_id)
            outcome = "appointment_booked" if booked else "callback"

        await publish(s.topic_calls_ended, {
            "eventType": "call_ended", "leadId": lead_id, "sessionId": session_id, "outcome": outcome,
            "durationSeconds": plan["duration"],
            "prospectProfile": {"lead_id": lead_id, "simulated": True, "outcome": outcome},
        }, key=lead_id)

    async def _book(self, lead_id: str, session_id: str) -> bool:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8080", timeout=15, headers=headers) as client:
            avail = (await client.get("/api/scheduling/availability", params={"limit": 3})).json()
            for start in avail.get("times") or []:
                r = await client.post("/api/appointments", json={
                    "lead_id": lead_id, "session_id": session_id, "start_time": start, "timezone": self._tz,
                    "prospect_profile": {"lead_id": lead_id, "simulated": True},
                })
                if r.status_code == 201:
                    return True
                if r.status_code != 409:
                    logger.warning("simulated booking → %s %s", r.status_code, r.text[:120])
                    return False
        return False
