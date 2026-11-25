# Eastwood

**A compliance-first AI outbound sales platform for insurance agencies.**

**Live overview:** [eastwood-two.vercel.app](https://eastwood-two.vercel.app)

Eastwood takes purchased or inbound leads, decides who may lawfully be contacted and who is worth contacting, and has **Imogen**, an AI assistant, work them. Text comes first: a two-way SMS conversation that qualifies the prospect, quotes only from verified carrier rate sheets, and books them with a licensed human agent who closes. A realtime voice mode runs the same discovery by phone when a campaign opts in. Every step is gated in code, not by prompting a model to behave.

> **Status: pre-launch.** The application is built and test-verified (782 unit and end-to-end tests, plus 26 database integration tests). It was verified end to end against an isolated stack with simulated providers: a 27,000-lead voice load test, and an SMS-first commercial acceptance test from upload to booked appointment (1,444 leads; every stated premium matched the rate book; no double booking). The remaining go-live work is credentials, registrations, verified carrier rates, data, and legal sign-off, in the order given by [`docs/operator-runbook.md`](docs/operator-runbook.md); see also [`docs/go-live-checklist.md`](docs/go-live-checklist.md), [`docs/sms-first-engine.md`](docs/sms-first-engine.md) and [`docs/launch-dependencies-and-costs.md`](docs/launch-dependencies-and-costs.md).

---

## How a lead moves through Eastwood

```
 ingest ─► normalize ─► [compliance gates] ─► triage & score ─► [campaign]
                                                                    │
     SMS-first (default): Imogen texts ─► qualify ─► [verified quote] ─► book ─┐
     voice (opt-in):      [dial queue] ─► Imogen's call ─► [quote] ─────► book ─┤
                                                                               ▼
                     appointment + full prospect profile ─► the licensed agent closes
```

1. **Ingest:** vendor CSVs (fuzzy column mapping), PDF lead exports, `POST /api/leads/ingest`, signed Facebook Lead Ads webhooks, and web forms.
2. **Normalize:** E.164 phones, product lines, age from DOB, and consent records. Unmapped columns are kept for audit.
3. **Compliance gates** (hard stops): TCPA 8am–9pm in the lead's local time, National DNC clearance (fail-closed), entity-specific opt-outs, and express written consent for auto-dial.
4. **Triage & score:** medical knockouts, severity scoring, and wealth/fit signals, then a route: `auto_dial`, `request_consent_sms`, `nurture`, `manual_review`, or `suppress`.
5. **Text conversation (default):** Imogen's first text names the agency, says she is an AI assistant, and says how to opt out. She asks only what the quote needs, handles objections and questions, and nudges twice if there is no reply. Every text comes from reviewed templates and passes a policy check; the model, when enabled, only reads unclear replies. See [`docs/sms-first-engine.md`](docs/sms-first-engine.md).
6. **Voice (opt-in per campaign):** a dial queue with a concurrency cap and retry ladder, and Imogen's realtime call, which runs discovery and health screening and captures only what she clearly heard.
7. **Quote** (hard stop): unlocks only after discovery. A premium reaches a prospect only from a **verified** carrier rate sheet and is stored with a quote reference. The engine picks the options; the model only explains them.
8. **Book & hand off:** a booking on the agent's own calendar (or Calendly), a confirmation and reminders, a persisted prospect profile, and a brief to the licensed agent. Outcome-aware follow-up sequences run afterward.

## Capabilities

### Lead intake
- Any vendor CSV or XLSX, mapped to canonical fields by fuzzy column matching
- PDF lead lists: ruled tables, column-aligned text, and scanned or photographed pages read by local OCR (Tesseract; lead data never leaves the server). Low-confidence rows are quarantined for review
- Upload-and-preview in the operator dashboard: see how every column will be read, fix the mapping, then import
- HTTP ingest API and signature-verified Facebook Lead Ads (`leadgen`) webhook
- Browser lead uploader (`landing/upload.html`) and a 60-second "risk scan" web funnel (`landing/site/`)
- Lead batches tied to vendor and cost for ROI tracking

### Bulk lead books
- CSV and XLSX import built for tens of thousands of rows: streaming readers, automatic column mapping, full state-name normalization, first-valid-of-several phone columns
- Idempotent (file hash) and resumable (cursor committed with each chunk; a killed import continues where it stopped)
- One lead per phone number; in-file repeats, existing numbers and previously opted-out numbers are reported, not inserted
- Owner, source, acquisition date, external id and consent evidence preserved per lead; per-row error report (row number + code, no personal data)
- Per-channel authorization: consent must be documented, name **your** agency, and cover AI voice (calls) or texts (SMS); anything else — including consent given to another seller — is quarantined with a reason
- Inventory by state, area code, product, lead age, consent and eligibility, with FTC registry cost and a coverage curve computed from the actual area codes

### Campaigns, budgets, and the calendar
- Leads are dialed only after an operator enqueues them into an active campaign
- Per-campaign concurrency and calls-per-minute under global caps; campaign budget ceiling that **halts outbound automatically**; daily spend ceiling
- Spend ledger (estimated from configurable unit prices) and a per-call attempt log
- The licensed agent's own part-time calendar: weekly windows, slot length, buffers, daily cap, blackout dates; overbooking is prevented by an advisory lock plus a unique index
- Operations report: attempts, answered calls, qualified prospects, appointments, opt-outs, conversion rates, cost per appointment

### Imogen by text (SMS-first, the default)
- Two-way conversation with stored history: qualify (coverage, age, tobacco, sex, the rate sheet's own health questions, budget), quote, handle objections and questions, book, confirm, remind (24 h and 2 h), reschedule and cancel
- Stops on a booking, a decline, a wrong number, or STOP. Hands off to the agent when the prospect asks for a person or the texts are not working. Optional escalation to an AI voice call
- Every outbound text passes a policy check: only stored quote figures, no guarantees, no Medicare "government benefit" claims, no SSN or bank requests, GSM-7 only, at most 3 segments, and a compliant first text
- Send-time gates: two-key outbound, SMS consent naming your agency, National DNC, suppression, 8am–9pm local, daily and campaign budgets, rate limits, at most once per step. Cost guards: one-segment openers, two nudges then dormant, a 30-text conversation cap, and outreach paused while the calendar is full
- Simultaneous choosers are offered different times, and the calendar never double-books

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
- **Prospects see verified rates only.** A carrier's own rate book (PDF with text, XLSX or CSV) is extracted into a rate sheet (`scripts/rates.py extract`: per-$1,000 rates or premium grids by face amount, with age, sex, tobacco, tier, policy fee, modal factors, rounding, effective dates and source document). It quotes only after spot checks from the carrier's own quote tool match to the cent, covering every rate class. Sources, access and costs per carrier: [`docs/carrier-quoting-matrix.md`](docs/carrier-quoting-matrix.md)
- Every option a prospect sees is stored with a quote id, a short `Q` reference, its source and rate sheet. A text can state only those figures. Quotes are labelled preliminary and never bindable
- With no verified sheet for a prospect, Imogen says she cannot give an exact rate and offers the agent. She never estimates
- Deterministic three-option logic (C need / B recommended / A most within budget)
- The formula and CMS adapters remain internal tools only (`PROSPECT_QUOTES_REQUIRE_VERIFIED=true`). Synthetic test sheets exist for testing and are refused in production

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
- **Two-key outbound launch:** nothing prospect-facing leaves the system unless `OUTBOUND_ENABLED=true` *and* an operator has authorized the campaign (`POST /api/outbound/resume`); `POST /api/outbound/halt` is the emergency stop. Every authorization and halt is recorded, and a fresh deployment starts halted
- Every prospect-facing send — dial, follow-up SMS or callback, consent request, booking confirmation — re-checks opt-out, National DNC, lead status, and the calling window at send time
- A texted STOP (signed `/twilio/sms` webhook) suppresses the number for calls and texts and cancels everything pending
- Production boot guards refuse to start with `BYPASS_TCPA=true`, a non-empty `DNC_TEST_ALLOWLIST`, no `EASTWOOD_API_KEY`, Twilio signature validation disabled, a non-FTC DNC provider, a temporary-tunnel or non-HTTPS `PUBLIC_BASE_URL`, scoring thresholds below 75/50, a rate-table reset, or no campaign states
- Only carrier rates with a recorded rate-sheet `source` are ever quoted as carrier rates; everything else is a labelled estimate
- Twilio `X-Twilio-Signature` validation against a canonical URL; Facebook signature enforcement; constant-time bearer auth between services; fail-closed route classification; tenant pinning; and OpenAPI docs disabled
- nginx exposes only signed webhooks and the media WebSocket; health, metrics, and session admin stay internal

### Operator APIs and observability
- Operator dashboard at `/ops` (core port only; not exposed by nginx): upload and map leads, create and launch campaigns, authorize or halt outbound, read conversations, see appointments with full profiles, and record attended, no-show, and sold outcomes with premium and commission
- Overview metrics: leads imported, texts sent and replies, active conversations, qualified prospects, quotes communicated, appointments and attendance, policies sold, spend, cost per booked appointment, commissions, and profit
- Closer cockpit (`/api/cockpit/queue`, `/api/cockpit/lead/{id}`): a best-first queue with placement and a suggested opener
- Batch ROI and lead-source ROI computed live, call-outcome tagging, and a dashboard summary
- 37 Prometheus metrics (27 agent, 10 core), a provisioned Grafana "Eastwood operations" dashboard, `scripts/turn_latency_report.sh`, and call-trace tooling
- Twilio answering-machine detection and an abandoned-session reaper

## What Eastwood does not do

- **Sell, bind, or apply.** Imogen is not licensed. No quote is bindable, no application is submitted, and no payment is taken. The appointment with a licensed agent is the close.
- **Pretend to be human.**
- **Dial without clearance:** never outside 8am–9pm local, never to a number that isn't explicitly `clear` or `exempt` on the DNC registry, and never auto-dial without express written consent.
- **Invent answers.** Unheard or unclear means the slot stays unknown.
- **Collect SSNs or bank details,** promise approval, or imply Medicare plans are a government benefit.
- **Invent prices.** A premium reaches a prospect only from a verified carrier rate sheet, with a stored quote id. Otherwise there is no number.
- **Name a carrier on an estimate,** or quote before discovery is complete.
- **Bridge live transfers automatically.** It texts the closer to call in.
- **Handle inbound calls or serve multiple tenants.** It is outbound-first and single-tenant. The operator UI is one dashboard page (`/ops`); the rest is JSON APIs and Grafana.

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
| Core service | Python 3.11, FastAPI, Uvicorn, Pydantic v2 + pydantic-settings, SQLAlchemy 2 (async) + asyncpg, Alembic, aiokafka, httpx, tenacity, twilio, phonenumbers, structlog, orjson, cryptography, prometheus-client, openpyxl, pdfplumber, pypdfium2 + Tesseract OCR |
| Voice agent | Node.js ≥ 20, TypeScript 5 (strict), Fastify 4 + @fastify/websocket, ws, XState 5, Zod, KafkaJS, Prisma 5, Pino, prom-client, tsx |
| AI | OpenAI Realtime `gpt-realtime-2` (voice "shimmer"), `gpt-4o-transcribe` with logprobs, tuned server VAD |
| Data | PostgreSQL 16 (two databases, 11 SQL migrations), Apache Kafka 3.7 (KRaft), Redis 7 |
| Infrastructure | Docker Compose (9 services), nginx 1.25, Prometheus, Grafana |
| Testing | pytest (512 unit tests incl. OCR, + 26 database integration tests), `node:test` via tsx (270 tests, including end-to-end call simulations), a 27,000-lead voice load test and an SMS-first acceptance test with simulated providers |
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
docs/            Operator runbook (start here); carrier quoting matrix; go-live checklist; launch
                 dependencies and costs; SMS-first engine; controlled live SMS test; import and
                 campaign runbook; capacity and load test
grafana/         Provisioned Prometheus datasource and operations dashboard
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

Leave `PUBLIC_BASE_URL` blank to create call sessions without placing real calls. Outbound is disarmed by default (`OUTBOUND_ENABLED=false`).

### Production

A separate env file and project keep production apart from the development stack:

```bash
cp .env.production.example .env.production && chmod 600 .env.production   # fill in
docker compose -p eastwood-prod --env-file .env.production \
  -f docker-compose.yml -f docker-compose.production.yml up -d --build
```

Compose refuses to start until every required credential is set, the boot guard re-checks the values that reach eastwood-core, all host ports bind to `127.0.0.1` (put a TLS proxy or named tunnel in front of nginx), and outbound stays disarmed until it is both armed and authorized.

## Tests

```bash
# eastwood-core
cd eastwood-core && pip install -r requirements.txt -r requirements-dev.txt && pytest -q

# ai-agent
cd ai-agent && npm ci && npx tsc --noEmit && npm test
```

## Compliance note

Eastwood is built to keep an agency inside TCPA, National DNC, and CMS marketing rules, but software cannot make a campaign compliant on its own. An operator must supply a real FTC registry subscription, verified consent, and licensed agents, and must work through [`docs/go-live-checklist.md`](docs/go-live-checklist.md) before any live dial. Never commit `.env`, lead lists, or registry data; `.gitignore` excludes them.
