from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from api.routes import router
from api.security import enforce_route_security
from config.settings import assert_production_safety, get_settings
from kafka_io.kafka_client import KafkaConsumer, stop_producer
from workers.brain import WorkersBrain

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

# Application loggers propagate to root. Without a root handler, Python's
# last-resort handler prints WARNING+ only, so every operational INFO line
# (dial fired, follow-up sent/skipped, brief skipped) was silently dropped.
import os as _os_logging

logging.basicConfig(
    level=_os_logging.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Fail before touching the database, Kafka, or the dialer.
    assert_production_safety(settings)

    if not settings.twilio_signature_validation_enabled:
        logger.warning(
            "================================================================\n"
            "TWILIO SIGNATURE VALIDATION IS DISABLED\n"
            "(TWILIO_SIGNATURE_VALIDATION_ENABLED=false)\n"
            "Every /twiml and /twilio route accepts unauthenticated requests.\n"
            "Development only — production refuses to boot in this state.\n"
            "================================================================"
        )
    if not settings.eastwood_api_key.strip():
        logger.warning(
            "EASTWOOD_API_KEY is not set — every authenticated /api route will "
            "refuse requests (fail closed). Set the same key here and in ai-agent."
        )

    # Ensure all ORM tables exist (idempotent — safe to run every startup)
    from sqlalchemy import text
    from db.models import Base
    from db.session import _get_engine
    async with _get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Idempotent column additions for pre-existing tables
        for sql in [
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS health_data JSONB NOT NULL DEFAULT '{}'",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS triage_bucket VARCHAR(32)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS triage_priority FLOAT",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS source_batch_id VARCHAR(64)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS ap_cents INTEGER",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_status VARCHAR(16) NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_checked_at TIMESTAMPTZ",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_provider VARCHAR(64)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_source VARCHAR(128)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS national_dnc_audit JSONB",
            "CREATE INDEX IF NOT EXISTS ix_leads_national_dnc_status ON leads(national_dnc_status)",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS prospect_email VARCHAR(256)",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS timezone VARCHAR(64)",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_event_uri TEXT",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_invitee_uri TEXT",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_cancel_url TEXT",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_reschedule_url TEXT",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS calendly_status VARCHAR(32) NOT NULL DEFAULT 'not_requested'",
            "ALTER TABLE carrier_rate_tables ADD COLUMN IF NOT EXISTS source VARCHAR(128)",
            # Lead provenance and per-channel authorization (bulk import)
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS lead_owner VARCHAR(128)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS external_id VARCHAR(128)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS acquired_at TIMESTAMPTZ",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS import_batch_id UUID",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS contact_authorization JSONB",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS quarantine_reasons JSONB",
            "CREATE INDEX IF NOT EXISTS ix_leads_import_batch_id ON leads(import_batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_leads_tenant_phone ON leads(tenant_id, phone_e164)",
            "CREATE INDEX IF NOT EXISTS ix_leads_status ON leads(status)",
            # Campaigns on the dial queue; the dialer's hot query
            "ALTER TABLE dial_queue ADD COLUMN IF NOT EXISTS campaign_id UUID",
            "ALTER TABLE dial_queue ADD COLUMN IF NOT EXISTS provider_call_sid VARCHAR(64)",
            # SMS-first engine, prospect-facing quotes, appointment outcomes
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS sex VARCHAR(1)",
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS tobacco BOOLEAN",
            "ALTER TABLE quote_records ADD COLUMN IF NOT EXISTS quote_ref VARCHAR(16)",
            "ALTER TABLE quote_records ADD COLUMN IF NOT EXISTS source VARCHAR(32)",
            "ALTER TABLE quote_records ADD COLUMN IF NOT EXISTS rate_sheet_id UUID",
            "ALTER TABLE quote_records ADD COLUMN IF NOT EXISTS conversation_id UUID",
            "ALTER TABLE quote_records ADD COLUMN IF NOT EXISTS communicated_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_quote_records_quote_ref ON quote_records(quote_ref)",
            "CREATE INDEX IF NOT EXISTS ix_quote_records_conversation_id ON quote_records(conversation_id)",
            "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS channel_strategy VARCHAR(16) NOT NULL DEFAULT 'sms_first'",
            "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS messages_per_minute INTEGER NOT NULL DEFAULT 20",
            "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS daily_message_cap INTEGER",
            "ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS escalate_to_voice BOOLEAN NOT NULL DEFAULT false",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS outcome VARCHAR(16)",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminders_sent JSONB",
            # statement time, not transaction start: a prospect's text and the
            # reply written in the same transaction keep their order
            "ALTER TABLE sms_messages ALTER COLUMN created_at SET DEFAULT clock_timestamp()",
            "ALTER TABLE rate_sheets ADD COLUMN IF NOT EXISTS policy_fee_monthly NUMERIC(10,2) NOT NULL DEFAULT 0",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS conversation_id UUID",
            "ALTER TABLE appointments ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS ix_dial_queue_campaign_id ON dial_queue(campaign_id)",
            "CREATE INDEX IF NOT EXISTS ix_dial_queue_due ON dial_queue(status, scheduled_at, priority DESC)",
            "CREATE INDEX IF NOT EXISTS ix_suppressions_tenant_phone ON suppressions(tenant_id, phone_e164)",
            "ALTER TABLE call_records ADD COLUMN IF NOT EXISTS ai_summary TEXT",
            "ALTER TABLE call_records ADD COLUMN IF NOT EXISTS health_flags JSONB",
            "ALTER TABLE call_records ADD COLUMN IF NOT EXISTS carrier_placed VARCHAR(128)",
            "CREATE INDEX IF NOT EXISTS ix_leads_source_batch_id ON leads(source_batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_followup_tasks_fire_at ON followup_tasks(fire_at)",
            "CREATE INDEX IF NOT EXISTS ix_followup_tasks_status ON followup_tasks(status)",
            "CREATE INDEX IF NOT EXISTS ix_carrier_profiles_product_line ON carrier_profiles(product_line)",
            "CREATE INDEX IF NOT EXISTS ix_carrier_profiles_is_active ON carrier_profiles(is_active)",
            "CREATE INDEX IF NOT EXISTS ix_lead_field_events_lead_id ON lead_field_events(lead_id)",
        ]:
            await conn.execute(text(sql))
    # No double-booking: one reserved slot per start time per tenant. Guarded,
    # because a database written by the old booking path may already hold
    # duplicate reservations, and a failed CREATE UNIQUE INDEX must not stop boot.
    async with _get_engine().begin() as conn:
        dupes = (await conn.execute(text(
            "SELECT count(*) FROM (SELECT 1 FROM booking_slots WHERE available = false "
            "GROUP BY tenant_id, starts_at HAVING count(*) > 1) d"
        ))).scalar_one()
        if dupes:
            logger.error(
                "booking_slots holds %d double-booked start times; the no-overbooking "
                "unique index was NOT created. Resolve them, then restart.", dupes,
            )
        else:
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_booking_slots_reserved "
                "ON booking_slots(tenant_id, starts_at) WHERE available = false"
            ))
    logger.info("database schema verified")

    # Seed v0.1 ruleset if the rulesets table is empty
    from datetime import date as _date
    import json as _json
    import pathlib as _pathlib
    from db.models import RulesetModel as _RulesetModel
    from sqlalchemy import select as _select
    _json_path = _pathlib.Path(__file__).parent.parent / "domain" / "triage" / "ruleset_v0_1.json"
    async with _get_engine().connect() as _conn:
        _existing = await _conn.execute(_select(_RulesetModel).limit(1))
        if _existing.first() is None and _json_path.exists():
            _payload = _json.loads(_json_path.read_text())
            async with _get_engine().begin() as _wconn:
                await _wconn.execute(
                    _RulesetModel.__table__.insert().values(
                        version=_payload["ruleset_version"],
                        effective_date=_date.fromisoformat(_payload["effective_date"]),
                        payload=_payload,
                        is_active=True,
                    )
                )
            logger.info("seeded ruleset v%s", _payload["ruleset_version"])

    # Seed v0.1 carrier profiles if the table is empty
    import json as _json2
    import pathlib as _pathlib2
    from db.models import CarrierProfileModel as _CPModel
    _cp_path = _pathlib2.Path(__file__).parent.parent / "domain" / "placement" / "carrier_profiles_v0_1.json"
    if _cp_path.exists():
        async with _get_engine().connect() as _cpconn:
            _cp_existing = await _cpconn.execute(_select(_CPModel).limit(1))
            if _cp_existing.first() is None:
                _cp_data = _json2.loads(_cp_path.read_text())
                _version = _cp_data["version"]
                async with _get_engine().begin() as _cpw:
                    for _p in _cp_data["profiles"]:
                        await _cpw.execute(
                            _CPModel.__table__.insert().values(
                                version=_version,
                                carrier_name=_p["carrier_name"],
                                product_line=_p["product_line"],
                                states=_p.get("states"),
                                min_age=_p["min_age"],
                                max_age=_p["max_age"],
                                has_gi=_p["has_gi"],
                                level_conditions=_p.get("level_conditions", []),
                                graded_conditions=_p.get("graded_conditions", []),
                                modified_conditions=_p.get("modified_conditions", []),
                                knockout_conditions=_p.get("knockout_conditions", []),
                                required_questions=_p.get("required_questions", []),
                                priority_weight=_p.get("priority_weight", 1.0),
                                is_active=True,
                            )
                        )
                logger.info("seeded %d carrier profiles (version=%s)", len(_cp_data["profiles"]), _version)

    # Carrier rate table — truncate if RESET_RATE_TABLE_ON_BOOT=true. Read
    # through Settings (not os.environ) so the production boot guard, which
    # refuses this flag, sees exactly the value acted on here.
    if settings.reset_rate_table_on_boot:
        async with _get_engine().begin() as _rtw:
            await _rtw.execute(text("TRUNCATE carrier_rate_tables"))
        logger.warning("RESET_RATE_TABLE_ON_BOOT: carrier_rate_tables truncated — formula adapter will be used for all quotes")
    # No automatic seeding. scripts/seed_rate_tables.py generates formula output
    # under real carrier names; seeding it at boot used to turn every quote into
    # a "carrier" quote naming a carrier. Carrier-rate mode (go-live checklist
    # §4 Mode B) requires loading validated rate sheets with a `source`.
    async with _get_engine().connect() as _rt_conn:
        _rt_carrier_rows = (await _rt_conn.execute(text(
            "SELECT count(*) FROM carrier_rate_tables "
            "WHERE is_active AND source IS NOT NULL AND source <> 'synthetic_seed'"
        ))).scalar_one()
    logger.warning(
        "QUOTE MODE: %s (%d active carrier rate rows with provenance)",
        "carrier-rate (Mode B)" if _rt_carrier_rows else "estimate-and-book (Mode A, formula estimates)",
        _rt_carrier_rows,
    )

    # Announce the outbound campaign state once at boot, loudly.
    from db.session import session_scope as _ss
    from domain.outbound import get_outbound_state
    async with _ss() as _os_session:
        _ob = await get_outbound_state(_os_session)
    logger.warning(
        "OUTBOUND CAMPAIGN STATE: permitted=%s reason=%s (OUTBOUND_ENABLED=%s)",
        _ob.permitted, _ob.reason, settings.outbound_enabled,
    )

    from kafka_io.kafka_client import get_producer
    await get_producer()

    brain = WorkersBrain()

    consumer = KafkaConsumer(
        group_id="eastwood-core",
        topics=settings.consumed_topics(),
    )

    @consumer.on(settings.topic_leads_raw)
    async def handle_raw(msg: dict):
        await brain.on_raw(msg)

    @consumer.on(settings.topic_leads_normalized)
    async def handle_normalized(msg: dict):
        await brain.score_and_route(msg)

    @consumer.on(settings.topic_agent_events)
    async def handle_agent_event(msg: dict):
        await brain.on_agent_event(msg)

    @consumer.on(settings.topic_calls_quote_requested)
    async def handle_quote_requested(msg: dict):
        await brain.on_quote_requested(msg)

    @consumer.on(settings.topic_calls_quote_presented)
    async def handle_quote_presented(msg: dict):
        await brain.on_agent_event({**msg, "eventType": "quote_presented"})

    @consumer.on(settings.topic_calls_field_captured)
    async def handle_field_captured(msg: dict):
        await brain.on_agent_event({**msg, "eventType": "field_captured"})

    @consumer.on(settings.topic_calls_objection_captured)
    async def handle_objection_captured(msg: dict):
        await brain.on_agent_event({**msg, "eventType": "objection_captured"})

    @consumer.on(settings.topic_calls_consent_captured)
    async def handle_consent_captured(msg: dict):
        await brain.on_agent_event({**msg, "eventType": "consent_captured"})

    @consumer.on(settings.topic_calls_dnc_requested)
    async def handle_dnc_requested(msg: dict):
        await brain.on_agent_event({**msg, "eventType": "dnc_request"})

    @consumer.on(settings.topic_calls_live_transfer_requested)
    async def handle_live_transfer_requested(msg: dict):
        await brain.on_agent_event({**msg, "eventType": "live_transfer_requested"})

    @consumer.on(settings.topic_calls_ended)
    async def handle_call_ended(msg: dict):
        await brain.on_agent_event(msg)

    await consumer.start()

    import asyncio as _asyncio

    # Follow-up task runner — drains due tasks every 30s
    async def _followup_loop_wrapped():
        while True:
            try:
                from domain.followup import run_due_tasks
                n = await run_due_tasks(
                    sms_client=brain._sms,
                    ai_agent_client=brain._ai_agent,
                )
                if n:
                    logger.info("followup worker fired %d tasks", n)
            except Exception:
                logger.exception("followup worker error")
            await _asyncio.sleep(30)

    # Dial-queue drain loop — fires queued calls every 3s
    async def _dial_loop_wrapped():
        while True:
            try:
                n = await brain.process_dial_queue()
                if n:
                    logger.info("dial worker fired %d calls", n)
            except Exception:
                logger.exception("dial worker error")
            await _asyncio.sleep(3)

    # Reaper — marks stuck 'dialing' entries as failed after 10 min
    async def _reaper_loop_wrapped():
        while True:
            try:
                from db.session import session_scope
                from repos.repos import DialQueueRepo
                async with session_scope() as _rs:
                    n = await DialQueueRepo(_rs).reap_stuck_dialing(older_than_minutes=10)
                    if n:
                        logger.warning("reaped %d stuck dial_queue entries", n)
                    from domain.dialing.reconcile import reconcile_stuck_calls
                    rec = await reconcile_stuck_calls(brain)
                    if rec.get("reconciled"):
                        logger.warning("reconciled %d calls whose status callback never arrived", rec["reconciled"])
                    from domain.attempts import reap_open_attempts
                    m = await reap_open_attempts(_rs, older_than_minutes=15)
                    if m:
                        logger.warning("closed %d dial attempts with no terminal callback", m)
            except Exception:
                logger.exception("reaper error")
            await _asyncio.sleep(60)

    # SMS-first engine: outreach, follow-ups, deferred replies, reminders.
    from domain.sms.engine import SmsEngine
    sms_engine = SmsEngine(brain._sms)
    app.state.sms_engine = sms_engine

    async def _sms_loop_wrapped():
        while True:
            try:
                counts = await sms_engine.run_due()
                if counts and any(k in counts for k in ("sent", "reminders_sent")):
                    logger.info("sms engine: %s", counts)
            except Exception:
                logger.exception("sms engine error")
            await _asyncio.sleep(5)

    _sms_task = _asyncio.create_task(_sms_loop_wrapped())
    _dial_task = _asyncio.create_task(_dial_loop_wrapped())
    _followup_task = _asyncio.create_task(_followup_loop_wrapped())
    _reaper_task = _asyncio.create_task(_reaper_loop_wrapped())

    yield

    _sms_task.cancel()
    _dial_task.cancel()
    _followup_task.cancel()
    _reaper_task.cancel()
    await consumer.stop()
    await stop_producer()
    await brain._ai_agent.aclose()


# Route security is applied app-wide from the explicit policy table in
# api.security — every route, including /health and /metrics, is classified.
# The docs/openapi endpoints are plain Starlette routes that app-level
# dependencies cannot protect, and the schema maps the whole attack surface,
# so they are disabled rather than left reachable without credentials.
app = FastAPI(
    title="Eastwood Core",
    lifespan=lifespan,
    dependencies=[Depends(enforce_route_security)],
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
