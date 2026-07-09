from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from api.routes import router
from config.settings import get_settings
from kafka_io.kafka_client import KafkaConsumer, stop_producer
from workers.brain import WorkersBrain

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

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

    # Carrier rate table — truncate if RESET_RATE_TABLE_ON_BOOT=true, otherwise seed if empty
    import os as _os
    from db.models import CarrierRateTable as _CRTModel
    if _os.environ.get("RESET_RATE_TABLE_ON_BOOT", "false").lower() == "true":
        async with _get_engine().begin() as _rtw:
            await _rtw.execute(text("TRUNCATE carrier_rate_tables"))
        logger.warning("RESET_RATE_TABLE_ON_BOOT: carrier_rate_tables truncated — formula adapter will be used for all quotes")
    else:
        async with _get_engine().connect() as _rt_conn:
            _rt_existing = await _rt_conn.execute(_select(_CRTModel).limit(1))
            if _rt_existing.first() is None:
                from scripts.seed_rate_tables import _all_seed_rows
                _rt_rows = _all_seed_rows()
                async with _get_engine().begin() as _rtw:
                    await _rtw.execute(_CRTModel.__table__.insert(), _rt_rows)
                logger.info("seeded %d carrier rate table rows", len(_rt_rows))

    from kafka_io.kafka_client import get_producer
    await get_producer()

    brain = WorkersBrain()

    consumer = KafkaConsumer(
        group_id="eastwood-core",
        topics=[
            settings.topic_leads_raw,
            settings.topic_leads_normalized,
            # legacy catch-all (kept while traffic migrates to discrete topics)
            settings.topic_agent_events,
            # discrete call-event topics
            settings.topic_calls_quote_requested,
            settings.topic_calls_field_captured,
            settings.topic_calls_objection_captured,
            settings.topic_calls_consent_captured,
            settings.topic_calls_dnc_requested,
            settings.topic_calls_live_transfer_requested,
            settings.topic_calls_ended,
        ],
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
            except Exception:
                logger.exception("reaper error")
            await _asyncio.sleep(60)

    _dial_task = _asyncio.create_task(_dial_loop_wrapped())
    _followup_task = _asyncio.create_task(_followup_loop_wrapped())
    _reaper_task = _asyncio.create_task(_reaper_loop_wrapped())

    yield

    _dial_task.cancel()
    _followup_task.cancel()
    _reaper_task.cancel()
    await consumer.stop()
    await stop_producer()
    await brain._ai_agent.aclose()


app = FastAPI(title="Eastwood Core", lifespan=lifespan)
app.include_router(router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    data = generate_latest()
    return Response(content=data, media_type=CONTENT_TYPE_LATEST)
