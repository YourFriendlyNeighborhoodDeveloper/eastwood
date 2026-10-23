# Eastwood

**A compliance-first AI outbound sales platform for insurance agencies.**

Eastwood takes purchased or inbound leads, decides who may lawfully be called and who is worth calling, and has **Imogen**, a realtime AI voice assistant, run the discovery call. She screens health, presents deterministic quotes, and books the prospect with a licensed human agent who closes. Every step is gated in code, not by prompting the model to behave.

> **Status: pre-launch.** The application is built and test-verified (453 tests passing). The remaining go-live work is configuration, credentials, and data; see [`docs/go-live-checklist.md`](docs/go-live-checklist.md).

---

## How a lead moves through Eastwood

```
 ingest ─► normalize ─► [compliance gates] ─► triage & score ─► [dial queue]
                                                                     │
   follow-ups ◄── book & hand off ◄── [quote] ◄── Imogen's discovery call
```

1. **Ingest:** vendor CSVs (fuzzy column mapping), PDF lead exports, `POST /api/leads/ingest`, signed Facebook Lead Ads webhooks, and web forms.
2. **Normalize:** E.164 phones, product lines, age from DOB, and consent records. Unmapped columns are kept for audit.
3. **Compliance gates** (hard stops): TCPA 8am–9pm in the lead's local time, National DNC clearance (fail-closed), entity-specific opt-outs, and express written consent for auto-dial.
4. **Triage & score:** medical knockouts, severity scoring, and wealth/fit signals, then a route: `auto_dial`, `request_consent_sms`, `nurture`, `manual_review`, or `suppress`.
5. **Dial queue** (hard stop): concurrency cap, a 4/24/32/56/80-hour retry ladder, max attempts, and idempotent Twilio status handling.
6. **Imogen's call:** discloses she is an AI, confirms identity, runs needs discovery and health screening, and captures only what she clearly heard.
7. **Quote** (hard stop): unlocks only after discovery. The engine picks three options; the model only explains them.
8. **Book & hand off:** a real Calendly booking, a persisted prospect profile, and an SMS brief to the licensed agent. Outcome-aware follow-up sequences run afterward.

## Capabilities

### Lead intake
- Any vendor CSV, mapped to canonical fields by fuzzy column matching; PDF table extraction feeds the same mapper
- HTTP ingest API and signature-verified Facebook Lead Ads (`leadgen`) webhook
- Browser lead uploader (`landing/upload.html`) and a 60-second "risk scan" web funnel (`landing/site/`)
- Lead batches tied to vendor and cost for ROI tracking

### Enrichment, scoring, and routing
- Optional OpenFEC donor and NPPES NPI provider lookups
- Home-value wealth tiers, per-product age-fit bands, consent freshness, state fit, and a recent-touch penalty
- Hot / warm / cold bands (`SCORE_HOT_THRESHOLD`, `SCORE_WARM_THRESHOLD`)

### Medical triage (field underwriting)
- **L0** compliance and contactability gate → **L1** universal knockouts (hospice, dementia, ALS, Huntington's, …) → confidence check → **L2** severity score → priority
- Guaranteed-issue floor conditions, comorbidity bumps, lookback windows, and medication proxies (insulin → diabetes, Eliquis → cardiac)
- `CALL_NOW` / `WORKABLE` / `DEAD` buckets from a versioned JSON ruleset

### Carrier placement
- Ranks carriers per lead with the likely rate tier (level, graded, modified, GI) and the questions still needed for each
- Nine profiles: American Amicable, Mutual of Omaha, Royal Neighbors, Transamerica, Prosperity Life, Aetna / CVS (MA + Medigap), and Humana (MA + Medigap)

### Imogen, the voice agent (`ai-agent/`)
- OpenAI Realtime speech-to-speech (`gpt-realtime-2`) bridged to Twilio Media Streams over WebSocket
- Opens as an AI and answers truthfully if asked; product scripts for final expense, life, annuity, Medicare Advantage, Medigap, and ACA
- **Turn gate:** the model may not speak until the turn is ruled `heard_answer`, `heard_unclear`, `heard_other`, or `not_heard`, so silence or line noise is never acknowledged as an answer
- **Slot state:** every required answer moves `UNKNOWN → HEARD → CONFIRMED`; only confirmed values reach the quote engine or the handoff brief
- **Transcript grounding:** stored values must trace to the prospect's own finalized transcript, scored with `gpt-4o-transcribe` logprob confidence
- **Discovery flow:** a derived state machine (purpose → beneficiary → burial/cremation → coverage → rating sex → tobacco → health) that owns which question comes next
- Tier-1 screening, plus Tier-2 condition follow-ups added when a condition is confirmed
- Barge-in (cancel speech and flush queued audio), a stall watchdog, escalation after repeated unheard turns, and per-turn latency instrumentation

### Tools (12), unlocked by data rather than by a stage flag
`update_lead_field`, `add_followup_questions`, `get_quote`, `select_quote_option`, `get_booking_slots`, `book_appointment`, `capture_consent`, `capture_objection`, `capture_dnc`, `transfer_live`, `transfer_to_human`, `end_call`. Availability is derived from what has been captured (`ai-agent/src/tools/toolGating.ts`). After a do-not-call request, only `end_call` remains.

### Quoting
- Deterministic three-option quotes (C minimum / B recommended / A maximum within budget) in `domain/quoting/options.py`
- Adapters: CMS Medicare data API, carrier rate tables, and formula estimates, merged by source priority
- Honesty labels persisted with every quote: `quote_type`, `rate_source`, `carrier_rate`, `bindable` (always `false`)
- The carrier name is hidden on estimates, and premium-per-$1k sanity bands veto implausible numbers

### Live policy engine
Blocks utterances that promise approval ("guaranteed", "can't be denied"), imply Medicare plans are a government benefit, read back SSNs or bank details, or offer a vague premium range when exact quotes exist.

### Booking, handoff, and follow-up
- Calendly Scheduling API: real availability and real bookings, idempotent, failing honestly when Calendly is unavailable
- `handoff_profiles` stores the full prospect profile; an SMS brief goes to the licensed agent, and an outcome SMS follows every connected call
- `transfer_live` sends the closer an SMS alert to call in now
- Follow-up sequences: missed call, voicemail left, interested-but-busy, no contact, quoted-not-closed, price objection, spouse objection, application not completed, and payment failed

### Compliance and security
- TCPA calling windows for all 50 states + DC
- Official FTC National DNC registry provider: downloads subscribed area codes, checksums and freshness-limits the dataset, and stores the SAN only as a fingerprint. A `local_csv` blocklist exists for development.
- Production boot guards refuse to start with `BYPASS_TCPA=true`, a non-empty `DNC_TEST_ALLOWLIST`, no `EASTWOOD_API_KEY`, or Twilio signature validation disabled
- Twilio `X-Twilio-Signature` validation against a canonical URL; Facebook signature enforcement; constant-time bearer auth between services; fail-closed route classification; tenant pinning; and OpenAPI docs disabled
- nginx exposes only signed webhooks and the media WebSocket; health, metrics, and session admin stay internal

### Operator APIs and observability
- Closer cockpit (`/api/cockpit/queue`, `/api/cockpit/lead/{id}`): a best-first queue with placement and a suggested opener
- Batch ROI and lead-source ROI computed live, call-outcome tagging, and a dashboard summary
- 34 Prometheus metrics (27 agent, 7 core), Grafana, `scripts/turn_latency_report.sh`, and call-trace tooling
- Twilio answering-machine detection and an abandoned-session reaper

## What Eastwood does not do

- **Sell, bind, or apply.** Imogen is not licensed. No quote is bindable, no application is submitted, and no payment is taken. The appointment with a licensed agent is the close.
- **Pretend to be human.**
- **Dial without clearance:** never outside 8am–9pm local, never to a number that isn't explicitly `clear` or `exempt` on the DNC registry, and never auto-dial without express written consent.
- **Invent answers.** Unheard or unclear means the slot stays unknown.
- **Collect SSNs or bank details,** promise approval, or imply Medicare plans are a government benefit.
- **Name a carrier on an estimate,** or quote before discovery is complete.
- **Bridge live transfers automatically.** It texts the closer to call in.
- **Handle inbound calls, serve multiple tenants, or ship an operator UI.** It is outbound-only and single-tenant, with JSON APIs and Grafana dashboards.

## Architecture

```
                          nginx (edge)
        /api · /twiml · /twilio · media WebSocket · landing
                │                               │
   ┌────────────▼───────────┐  bearer   ┌───────▼────────────────┐
   │ eastwood-core          │◄── HTTP ─►│ ai-agent (Imogen)      │
   │ Python · FastAPI       │           │ TypeScript · Fastify   │
   └────────────┬───────────┘           └───────┬────────────────┘
                └───── Kafka 3.7 · 17 topics ───┘
                │                               │
      PostgreSQL 16 (eastwood)        PostgreSQL 16 (agent)
                 Redis 7 · Prometheus · Grafana

 External: Twilio (Voice/SMS/Media Streams/AMD) · OpenAI Realtime · Calendly
           CMS Data API · FTC DNC Registry · OpenFEC · NPPES NPI · Facebook Lead Ads
```

## Tech stack

| Layer | Technologies |
| --- | --- |
| Core service | Python 3.11, FastAPI, Uvicorn, Pydantic v2 + pydantic-settings, SQLAlchemy 2 (async) + asyncpg, Alembic, aiokafka, httpx, tenacity, twilio, phonenumbers, structlog, orjson, cryptography, prometheus-client |
| Voice agent | Node.js ≥ 20, TypeScript 5 (strict), Fastify 4 + @fastify/websocket, ws, XState 5, Zod, KafkaJS, Prisma 5, Pino, prom-client, tsx |
| AI | OpenAI Realtime `gpt-realtime-2` (voice "shimmer"), `gpt-4o-transcribe` with logprobs, tuned server VAD |
| Data | PostgreSQL 16 (two databases, 11 SQL migrations), Apache Kafka 3.7 (KRaft), Redis 7 |
| Infrastructure | Docker Compose (9 services), nginx 1.25, Prometheus, Grafana |
| Testing | pytest (188 tests), `node:test` via tsx (265 tests, including end-to-end call simulations) |
| Legacy pipeline (`src/`) | kafka-python, psycopg 3, Redis idempotency keys, booking tokens, consent evidence packs |

## Repository layout

```
eastwood-core/   Python core: API, domain logic (triage, quoting, compliance, dialing,
                 follow-up, scheduling, notifications), workers, tests
ai-agent/        TypeScript voice agent: realtime bridge, discovery, tools, policy, tests
src/             Earlier Kafka pipeline (normalize → validate → score → route)
migrations/      PostgreSQL schema history
config/          Legacy pipeline config and event JSON schemas
landing/         Static landing page, lead uploader, and risk-scan funnel (served by nginx)
nginx/           Edge proxy config
scripts/         DB init, migrations, turn-latency report
showcase/        The public capabilities site (deployed to Vercel)
docs/            Go-live checklist
```

## Running locally

```bash
cp .env.example .env          # fill in Twilio / OpenAI / API key values
docker compose up --build     # Kafka, Postgres, Redis, core, agent, nginx, Prometheus, Grafana
```

| Port | Service |
| --- | --- |
| `18000` | nginx edge |
| `18080` | eastwood-core |
| `18081` | ai-agent |
| `19090` | Prometheus |
| `13000` | Grafana |

Leave `PUBLIC_BASE_URL` blank to create call sessions without placing real calls.

## Tests

```bash
# eastwood-core
cd eastwood-core && pip install -r requirements.txt -r requirements-dev.txt && pytest -q

# ai-agent
cd ai-agent && npm ci && npx tsc --noEmit && npm test
```

## Compliance note

Eastwood is built to keep an agency inside TCPA, National DNC, and CMS marketing rules, but software cannot make a campaign compliant on its own. An operator must supply a real FTC registry subscription, verified consent, and licensed agents, and must work through [`docs/go-live-checklist.md`](docs/go-live-checklist.md) before any live dial. Never commit `.env`, lead lists, or registry data; `.gitignore` excludes them.
