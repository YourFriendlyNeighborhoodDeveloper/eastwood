---
name: verify
description: Build, launch, and drive the Eastwood stack to verify changes at its real surfaces (eastwood-core HTTP API, Kafka events, Postgres state).
---

# Verifying Eastwood changes

## Safety first

The repo-root `.env` contains **live Twilio credentials** and is read by
`docker compose`. Booting eastwood-core with them can send real SMS (default
agent-notification destination is Ryan's phone) and, with `PUBLIC_BASE_URL`
set, place real calls. Always blank them for verification runs:

```bash
TWILIO_ACCOUNT_SID= TWILIO_AUTH_TOKEN= TWILIO_FROM_NUMBER= PUBLIC_BASE_URL= \
  docker compose up -d --build eastwood-core
```

With Twilio blanked, SMS/dial paths log honestly ("SMS client not configured —
… skipped") instead of sending — grep the logs for those lines as evidence the
notifier fired.

## Handles

- **eastwood-core HTTP**: `http://localhost:18080` (compose maps 18080→8080).
  `until curl -sf localhost:18080/health; do sleep 1; done` to wait for boot.
  Booting the app applies the schema (create_all + idempotent ALTERs), so no
  separate migration step is needed.
- **Kafka**: no host port. Publish from inside the container:
  ```bash
  echo '<one-line json>' | docker exec -i eastwood-kafka-1 \
    /opt/bitnami/kafka/bin/kafka-console-producer.sh \
    --bootstrap-server localhost:9092 --topic calls.ended
  ```
  Event payloads use camelCase keys (`eventType`, `leadId`, `sessionId`).
- **Postgres**: `docker exec eastwood-postgres-1 psql -U eastwood -d eastwood -c "..."`.
  Create a throwaway lead directly (`INSERT INTO leads (id, tenant_id,
  phone_e164, status, health_data, national_dnc_status) VALUES
  (gen_random_uuid(), 'default', '+15555550999', 'nurture', '{}', 'unknown')`)
  — `national_dnc_status='unknown'` keeps it undialable.
- **Env overrides** (e.g. Calendly): compose only forwards variables listed in
  the service `environment:` block, so extra settings need an override file:
  `docker compose -f docker-compose.yml -f /tmp/override.yml up -d eastwood-core`.
  A bogus `CALENDLY_ACCESS_TOKEN` drives the honest-failure path (401 from the
  real API) without booking anything.

## Flows worth driving

- `POST /api/quote` with `coverage_purpose`/`desired_coverage_amount`/
  `monthly_budget` → expect the three-option `options` array (C/B/A, honesty
  labels, `within_budget`).
- `GET /api/scheduling/availability` → Calendly enabled/disabled honesty.
- `POST /api/appointments` (plain and with `start_time`+`email`) → row in
  `appointments` (calendly columns), `handoff_profiles` row, agent-brief log.
- Publish `calls.ended` with a `prospectProfile` → `handoff_profiles` row,
  outcome-SMS log line, `followup_tasks` rows whose `context_data` carries the
  persisted quote.

## Cleanup

Delete verification rows or pending `followup_tasks` will text/call the fake
number once Twilio is re-enabled: delete `appointments` (FK has no cascade),
then the lead (cascades followup_tasks/handoff_profiles/dial_queue), then
orphaned `booking_slots`.

ai-agent has no drivable surface without OpenAI + Twilio media streams; its
realtime flow is exercised by the e2e harness in `ai-agent/src/tests/` (fake
OpenAI socket, fake telephony), which is the closest available handle.
