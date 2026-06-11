import json
import logging
import os
import signal
import time
from datetime import datetime, timezone

import psycopg2
from kafka import KafkaConsumer, KafkaProducer


_STOP = False


def _handle_stop(_sig, _frame):
    global _STOP
    _STOP = True


def _now_utc():
    return datetime.now(timezone.utc)


def _setup_logging():
    level = os.getenv("EASTWOOD_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return logging.getLogger("eastwood.persist")


def _pg_conn():
    host = os.getenv("POSTGRES_HOST", "postgres")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    db = os.getenv("POSTGRES_DB", "eastwood")
    user = os.getenv("POSTGRES_USER", "eastwood")
    pw = os.getenv("POSTGRES_PASSWORD", "eastwood")
    conn = psycopg2.connect(host=host, port=port, dbname=db, user=user, password=pw)
    conn.autocommit = False
    return conn


def _consumer():
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    group_id = os.getenv("EASTWOOD_PERSIST_GROUP", "eastwood-persist")
    client_id = os.getenv("EASTWOOD_PERSIST_CLIENT_ID", "eastwood-persist")
    topic = os.getenv("EASTWOOD_TOPIC_ROUTED", "leads.routed")
    max_poll = int(os.getenv("EASTWOOD_PERSIST_MAX_POLL", "200"))
    poll_ms = int(os.getenv("EASTWOOD_PERSIST_POLL_MS", "1000"))

    c = KafkaConsumer(
        bootstrap_servers=bootstrap,
        group_id=group_id,
        client_id=client_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda b: b.decode("utf-8", errors="replace"),
        max_poll_records=max_poll,
        consumer_timeout_ms=poll_ms,
    )
    c.subscribe([topic])
    return c


def _producer():
    bootstrap = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
    client_id = os.getenv("EASTWOOD_PERSIST_CLIENT_ID", "eastwood-persist") + "-dlq"
    return KafkaProducer(
        bootstrap_servers=bootstrap,
        client_id=client_id,
        acks="all",
        retries=10,
        linger_ms=25,
        value_serializer=lambda d: json.dumps(d, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
    )


def _emit_dlq(log, prod, reason, raw, error=None):
    if prod is None:
        return
    topic = os.getenv("EASTWOOD_TOPIC_DLQ", "dlq.events")
    evt = {
        "event_id": os.urandom(16).hex(),
        "ts": _now_utc().isoformat(),
        "topic": os.getenv("EASTWOOD_TOPIC_ROUTED", "leads.routed"),
        "reason": reason,
        "payload": raw,
        "error": error,
    }
    try:
        prod.send(topic, evt)
    except Exception as e:
        log.warning("dlq send failed reason=%s err=%s", reason, str(e))


def _parse_dt(v):
    if not v:
        return _now_utc()
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc)
    if isinstance(v, str):
        s = v.replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc)
        except Exception:
            return _now_utc()
    return _now_utc()


def _upsert_lead(cur, lead):
    lead_id = lead.get("lead_id")
    product = lead.get("product")
    phone_e164 = lead.get("phone_e164")
    score = lead.get("score")
    routed_to = lead.get("routed_to")
    created_at = _parse_dt(lead.get("created_at"))

    status = "routed" if routed_to else "scored"
    updated_at = _now_utc()

    cur.execute(
        """
        insert into leads (lead_id, product, phone_e164, status, score, routed_to, created_at, updated_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (lead_id) do update
        set product = excluded.product,
            phone_e164 = excluded.phone_e164,
            status = excluded.status,
            score = excluded.score,
            routed_to = excluded.routed_to,
            updated_at = excluded.updated_at
        """,
        (lead_id, product, phone_e164, status, score, routed_to, created_at, updated_at),
    )


def run():
    log = _setup_logging()
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    log.info("starting")

    conn = None
    consumer = None
    prod = None

    pg_fail_sleep = 0.5
    kafka_fail_sleep = 0.5

    while not _STOP:
        try:
            if conn is None or conn.closed:
                conn = _pg_conn()
                pg_fail_sleep = 0.5
                log.info("postgres connected")

            if consumer is None:
                consumer = _consumer()
                kafka_fail_sleep = 0.5
                log.info(
                    "kafka consumer ready group=%s topic=%s",
                    os.getenv("EASTWOOD_PERSIST_GROUP", "eastwood-persist"),
                    os.getenv("EASTWOOD_TOPIC_ROUTED", "leads.routed"),
                )

            if prod is None and os.getenv("EASTWOOD_PERSIST_DLQ", "1") == "1":
                prod = _producer()
                log.info("dlq producer ready topic=%s", os.getenv("EASTWOOD_TOPIC_DLQ", "dlq.events"))

            records = consumer.poll(timeout_ms=int(os.getenv("EASTWOOD_PERSIST_POLL_MS", "1000")))
            batch = []
            for _, msgs in records.items():
                batch.extend(msgs)

            if not batch:
                time.sleep(0.1)
                continue

            upserts = 0
            skipped = 0

            with conn.cursor() as cur:
                for msg in batch:
                    raw = msg.value
                    try:
                        lead = json.loads(raw)
                    except Exception as e:
                        skipped += 1
                        _emit_dlq(log, prod, "persist_json_invalid", raw, str(e))
                        continue

                    if not isinstance(lead, dict):
                        skipped += 1
                        _emit_dlq(log, prod, "persist_payload_not_object", raw, None)
                        continue

                    if not lead.get("lead_id"):
                        skipped += 1
                        _emit_dlq(log, prod, "persist_missing_lead_id", raw, None)
                        continue

                    try:
                        _upsert_lead(cur, lead)
                        upserts += 1
                    except Exception as e:
                        skipped += 1
                        _emit_dlq(log, prod, "persist_db_upsert_failed", raw, str(e))

            conn.commit()
            consumer.commit()

            log.info("committed batch size=%d upserts=%d skipped=%d", len(batch), upserts, skipped)

        except psycopg2.Error as e:
            try:
                if conn is not None and not conn.closed:
                    conn.rollback()
            except Exception:
                pass
            log.error("postgres error: %s", str(e))
            try:
                if conn is not None and not conn.closed:
                    conn.close()
            except Exception:
                pass
            conn = None
            time.sleep(pg_fail_sleep)
            pg_fail_sleep = min(pg_fail_sleep * 2.0, 10.0)

        except Exception as e:
            try:
                if conn is not None and not conn.closed:
                    conn.rollback()
            except Exception:
                pass
            log.error("worker error: %s", str(e))
            try:
                if consumer is not None:
                    consumer.close()
            except Exception:
                pass
            consumer = None
            time.sleep(kafka_fail_sleep)
            kafka_fail_sleep = min(kafka_fail_sleep * 2.0, 10.0)

    log.info("stopping")

    try:
        if consumer is not None:
            consumer.close()
    except Exception:
        pass

    try:
        if prod is not None:
            prod.flush(timeout=5)
            prod.close()
    except Exception:
        pass

    try:
        if conn is not None and not conn.closed:
            conn.close()
    except Exception:
        pass

    log.info("stopped")


if __name__ == "__main__":
    run()