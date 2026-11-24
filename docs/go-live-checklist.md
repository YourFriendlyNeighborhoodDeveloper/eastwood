# Eastwood Go-Live Checklist

Operator-facing steps to take Eastwood from its current tested state to **one
controlled live campaign**. Work top to bottom. The system is GO only when every
required box is checked (§10).

Conventions:
- **Development** variables live in the repo-root `.env` (start from
  `.env.example`). **Production** uses a separate `.env.production` (start from
  `.env.production.example`) with the production overlay:
  `docker compose -p eastwood-prod --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml up -d --build`.
  Both env files are gitignored; keep them `chmod 600`.
- Schema is applied automatically when `eastwood-core` boots (idempotent
  `create_all` + `ALTER` in `app/main.py`); the SQL under `migrations/postgres/`
  is the record, not a manual step.
- Prometheus scrapes `eastwood-core:/metrics` and `ai-agent:/metrics`; Grafana is
  provisioned with a Prometheus datasource and the **Eastwood operations**
  dashboard (`grafana/`).
- Costs and third-party prerequisites: [`launch-dependencies-and-costs.md`](launch-dependencies-and-costs.md).

## Verification record — 2026-11-24

Everything below was run against an isolated stack: separate compose project
and database, no Twilio account SID (no call/SMS client can exist), Twilio and
OpenAI hostnames pinned to loopback, synthetic leads on fictional 555-01xx
numbers, Twilio webhooks simulated with signed requests. No real person was
contacted. A box is checked only when the result fully satisfies it; results
from simulation are noted inline, and live-provider steps stay open.

| Area | Result |
| --- | --- |
| All 9 services | up; core + agent `/health` 200; Postgres (both DBs), Redis `PONG`, Kafka 17 topics + both consumer groups, Prometheus 2/2 targets up, Grafana datasource + dashboard |
| Tests | eastwood-core **273 passed**, ai-agent **265 passed**, TypeScript clean |
| Lead pipeline | API ingest (401/403/422 enforced) → E.164 normalization → DNC scrub → triage → scoring → routing |
| Consent | no express written consent → never queued; consent request queued as a gated task, never sent inline |
| DNC | `unknown` never queued (status `awaiting_dnc_clearance`); follow-up to an unscrubbed number blocked |
| Campaign gate | armed-but-never-authorized holds dial queue and follow-ups; resume → dials; halt → nothing fires; state persists across restarts |
| Webhook smoke (simulated Twilio, via nginx) | signed TwiML 200 with media URL; unsigned 403; spoofed origin 403; media WebSocket 101; internal agent endpoints 404 at edge; no-answer → slot released + retry at +4.0 h; duplicate callback idempotent; completed → terminal; agent sessions return to 0 |
| Agent call (simulated events) | field capture audit row; quote presented + persisted; booking + handoff profile; `call_ended` → outcome follow-ups carrying the quote |
| Quotes | C/B/A, `estimated` / `formula_adapter`, `bindable=false`; carrier `null` to the model on estimates |
| Opt-out | mid-call DNC suppresses the dialed number (not the misheard one) and cancels retries; SMS STOP suppresses and cancels everything; STOP before import pre-suppresses; re-import queues nothing |
| Recovery | core restart (state intact); Kafka outage (ingest fails loudly, core stays up, consumer resumes); stuck `dialing` slot reaped; ai-agent restart |
| Production fail-closed | real image refuses to boot for each of: TCPA bypass, DNC allowlist, no API key, `local_csv`, ngrok URL, 5/5 thresholds, rate-table reset, invalid state; a valid production config boots **disarmed**; the production overlay refuses to start without external credentials |

Defects found and fixed in that run (commit `bdcbebb`): follow-ups and
consent SMS bypassed every compliance gate; texted STOP never suppressed
calls; synthetic seed rates were quoted as named carrier rates; Prometheus
scraped a non-existent target; compose never forwarded DNC/state/threshold
settings to the container; app INFO logs were dropped; a DNC-refused lead
reported `dialing`.

---

## 1. Secrets and account safety

- [ ] Rotate the **OpenAI API key** (issue a new key, then revoke the old one).
- [ ] Rotate the **Twilio auth token** (Twilio console → rotate; update any other
      consumers first).
- [ ] Update local `.env` with the new `OPENAI_API_KEY` and `TWILIO_AUTH_TOKEN`
      (and the `EASTWOOD_TWILIO_*` mirrors).
- [ ] Verify the **old keys are disabled** (an API call with the old key must fail).
      **Audit 2026-11-24:** a hash comparison (no values printed) shows the
      `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `EASTWOOD_TWILIO_AUTH_TOKEN` and
      `OPENAI_API_KEY` in the local `.env` are **identical to the values in the
      pre-scrub history** — none has been rotated.
- [x] Confirm **no secrets appear in currently tracked files**:
      `git grep -nE 'sk-(proj-)?[A-Za-z0-9]|AC[0-9a-f]{32}' -- . ':!.env.example'`
      should return nothing. *Verified 2026-11-24: 0 matches in tracked files and
      in all reachable commits; `origin/main` equals the scrubbed local history;
      both built images contain no `.env`, lead CSV or `data/private` files.*
- [x] Local `.env` permissions tightened to `600` (was world-readable `644`);
      `.env.production` created `600` with generated internal secrets only.
- [x] **Git history scrubbed (2026-07-16).** `git filter-repo` removed the old
      `.env` (live Twilio token + OpenAI key), the committed lead-export CSV
      (real PII), and `ai-agent/node_modules` from every commit, and replaced
      the leaked credential literals anywhere else they appeared. Verified: the
      secret grep above returns nothing across all reachable history
      (`git rev-list --all`), `git fsck --unreachable` is empty, and the repo
      has never had a remote — no external copy exists. Every commit SHA
      changed in the rewrite. **The keys themselves are still live** — the
      rotation boxes above remain the blocker.
- [ ] **Delete the pre-scrub backup after rotation is verified.**
      `~/Desktop/Eastwood-pre-scrub-2026-07-16.bundle` holds the un-scrubbed
      history (secrets + lead PII) as a safety net for the rewrite. Once the
      old credentials are confirmed dead and the repo state is good, remove it.
- [x] **Phone number scrubbed (2026-10-22), before the first push.** A real
      phone number hardcoded as the agent-notification default (settings,
      legacy `config/config.yaml`, and three test files) was replaced in
      every commit with `git filter-repo`; tests now use a fictional
      `555-01xx` number. Every commit SHA changed again.
- [ ] **Delete the second backup once the GitHub repo is verified.**
      `~/Desktop/Eastwood-pre-phone-scrub-2026-10-22.bundle` still holds the
      number.

---

## 2. National DNC readiness

The dialer will **not** call a lead unless `national_dnc_status` is `clear` or
`exempt`. New leads default to `unknown`, which is not dialable. With the default
`local_csv` provider nothing ever resolves to `clear`, so **you must configure the
official provider before any real dial.** Production now refuses to boot
without `DNC_SCRUB_PROVIDER=ftc_registry`, `DNC_SAN` and `DNC_AREA_CODES`, and
`docker-compose.yml` now forwards the `DNC_*` settings (previously they never
reached the container) and keeps the dataset in the `dncdata` volume.

The subscription is priced **per phone area code** (first five free), and a
lead's area code often differs from its state. A number whose area code is not
subscribed stays `unknown` and is never dialed, so the subscribed area codes
also define the campaign's reach. Leads scored before the first successful
refresh sit at `awaiting_dnc_clearance`; re-ingest them after the refresh to
re-scrub and queue them.

- [ ] Obtain / verify the **FTC registry account and SAN** (Subscription Account
      Number).
- [ ] Confirm the **subscribed area codes cover every area code in the imported
      lead list** (a number whose area code was not downloaded resolves to
      `unknown` → not dialable).
- [ ] Set in `.env`:
  - `DNC_SCRUB_PROVIDER=ftc_registry`
  - `DNC_SAN=…`
  - `DNC_REGISTRY_USERNAME=…`
  - `DNC_REGISTRY_PASSWORD=…`
  - `DNC_AREA_CODES=…` (comma-separated, e.g. `313,248,586`)
  - `DNC_REGISTRY_DOWNLOAD_URL=…` (the documented FTC automated-download endpoint;
    operator-supplied — the code does not hardcode it)
  - `DNC_DATA_DIRECTORY=data/private/dnc_registry`
  - `DNC_MAX_DATA_AGE_DAYS=31`
- [ ] Run the refresh: `python scripts/refresh_dnc.py` (from `eastwood-core/`, or
      `docker compose exec eastwood-core python scripts/refresh_dnc.py`). It must
      print an OK line with the area codes and number count. A "refused" line means
      SAN / URL is missing.
- [ ] Verify the dataset in `data/private/dnc_registry/manifest.json`:
  - **freshness** — `downloaded_at` within `DNC_MAX_DATA_AGE_DAYS`
  - **completeness** — `"complete": true`
  - **scope** — `area_codes` matches `DNC_AREA_CODES`
  - **checksum** — every entry under `files` has a `sha256:` checksum
  - **SAN fingerprint** — `san_fingerprint` is present (a hash, never the raw SAN)
- [ ] Confirm test leads resolve to `blocked` (a number you know is on the
      registry) or `clear` (a number you know is not, in a subscribed area code).
- [ ] Confirm `unknown` leads remain **non-dialable** (an out-of-scope area code,
      or before a successful refresh, must not dial).
- [ ] Schedule the refresh to run more often than `DNC_MAX_DATA_AGE_DAYS`, or the
      dataset will age out and stop clearing numbers mid-campaign.

> The registry blocklist and downloaded data live only under `data/private/`
> (gitignored). Never commit registry data; it is never logged or returned via API.

---

## 3. TCPA and dialing configuration

The production overlay (`docker-compose.production.yml`) pins the first five
items regardless of what `.env.production` says, and the boot guard refuses each
unsafe value (verified in the real image, 2026-11-24).

- [x] `APP_ENV=production` *(pinned by the production overlay)*
- [x] `BYPASS_TCPA=false` *(pinned)*
      — With `APP_ENV=production` **and** `BYPASS_TCPA=true`, `eastwood-core`
      refuses to boot (compliance guard). A clean boot confirms this is off.
- [x] `DNC_TEST_ALLOWLIST` empty *(pinned)*
      — The dev test allowlist scrubs self-owned numbers as `exempt` without a
      provider. In production it must be empty; `eastwood-core` refuses to boot
      with `APP_ENV=production` and a non-empty allowlist (same compliance guard).
- [x] `SCORE_HOT_THRESHOLD=75` *(template default; production refuses lower)*
- [x] `SCORE_WARM_THRESHOLD=50` *(template default; production refuses lower)*
- [ ] **Verify state timezone calling windows** — dialing is gated to 8am–9pm local
      time per the lead's state (`domain/tcpa.py`). Spot-check a lead in each
      campaign state. *MI verified 2026-11-24; follow-ups now defer to the next
      window too. The code enforces the federal 8am–9pm window only — check each
      campaign state for a stricter state-law window before adding it.*
- [x] **Verify entity-specific suppression checks** — a prior "stop calling"
      request (the `suppressions` table) blocks both dialing and re-import.
      *Verified: mid-call DNC, texted STOP (`/twilio/sms`), STOP before import,
      re-import, and follow-ups to suppressed numbers.*
- [x] **Verify national DNC checks** — see §2; `unknown`/`blocked` never dial.
      *Verified: `unknown` never queued; follow-ups to unscrubbed numbers blocked.*
- [x] **Verify callable phone normalization** — every lead has a valid E.164
      `phone_e164`; rows without one were dropped at ingest. *Verified (invalid
      number → 422).*
- [x] **Verify max concurrency and retry limits** *(verified 2026-11-24:
      `MAX_CONCURRENT_CALLS=1` fired exactly one call; no-answer rescheduled at
      +4.0 h; duplicate callback did not reschedule; stuck slot reaped; full
      ladder and max attempts covered by `tests/test_retry_ladder.py`)*:
  - `MAX_CONCURRENT_CALLS` (hard cap on simultaneous calls)
  - `DIAL_MAX_ATTEMPTS` (attempts per lead)
  - retry ladder `_RETRY_OFFSETS_HOURS = [4, 24, 32, 56, 80]` (`domain/dialing`)
- [ ] **Verify `ACTIVE_STATES` includes every campaign state.** Auto-dial routing
      depends on scoring; a lead outside `ACTIVE_STATES` loses its state-fit points
      and may route to `nurture` instead of `auto_dial`. This is the most common
      reason "nothing dials" after import. *Unknown codes are now refused at
      startup; production refuses an empty list. Choosing the states is yours.*
- [ ] **Run a dry-run eligibility report before enabling Twilio.** Import the list
      with `PUBLIC_BASE_URL` unset (sessions are created but no call is placed), or
      query `dial_queue`/`leads` directly, and confirm the leads you expect to dial
      actually reach `action=auto_dial` / `status=dialing` and pass every gate. If
      they route to `nurture`, revisit `ACTIVE_STATES` and the score thresholds
      before touching the phone.

---

## 4. Quote mode

Choose and document **exactly one** launch mode. Ambiguous mixed mode is not
permitted.

> **Fixed 2026-11-24:** with `RESET_RATE_TABLE_ON_BOOT=false`, boot used to seed
> formula-generated "sample" rates under real carrier names, so every quote was
> labelled a carrier rate and named a carrier. Nothing is seeded at boot now,
> and only rows with a real `source` are quoted as carrier rates. Boot logs the
> active mode: `QUOTE MODE: estimate-and-book …` or `carrier-rate (Mode B)`.

### Mode A — estimate-and-book (recommended for first launch)
- [x] `RESET_RATE_TABLE_ON_BOOT=false` *(pinned; with no provenance rows the
      formula adapter is used)*
- [x] `FormulaAdapter` output remains labeled `quote_type=estimated` *(verified)*
- [x] `carrier_rate=false` *(verified)*
- [x] `bindable=false` *(verified)*
- [x] Carrier name stays **hidden from the prospect** (the tool returns `carrier:
      null` for estimates) *(`toolHandlers.ts`; ai-agent quote tests)*
- [ ] Imogen uses the approved estimate language: *"Based on what you told me, we
      may be looking around $X per month. I'd like to book a licensed review to
      confirm the exact carrier and final rate."*

### Mode B — carrier-rate mode
- [ ] Load validated carrier rate tables (`carrier_rate_tables`) with
      `RESET_RATE_TABLE_ON_BOOT=false`. Every row needs a `source` naming the
      rate sheet (rows with no source, or `synthetic_seed`, are ignored). There
      is no import tool yet; loading is a manual, reviewed step.
- [ ] Verify each row's `source` / `effective_date` (there is no `version` column)
- [ ] Confirm quote labels show `rate_source=rate_table` (or `cms_api`) and
      `carrier_rate=true`
- [ ] `bindable` remains `false` (Eastwood submits no application; nothing binds)
- [ ] Run a sample comparison against carrier quoting portals for a spread of
      ages/face amounts before trusting the numbers on a call

> Whichever mode: no quote Eastwood produces is bindable. The appointment with a
> licensed agent is the close.

---

## 5. Public webhook infrastructure

### Completed — webhook authentication and API auth (commit `6f2cff8`)

Implemented and verified 2026-07-16. Do not re-audit; re-verify only if these
surfaces change. The policy lives in `eastwood-core/api/security.py` and its
tests in `eastwood-core/tests/test_route_security.py` and
`ai-agent/src/tests/coreAuth.test.ts`.

- [x] Centralized fail-closed route classification and enforcement (one policy
      table, one app-wide dependency; unlisted routes default to authenticated)
- [x] Twilio signature validation using the official `RequestValidator` —
      missing or invalid `X-Twilio-Signature` returns 403 on `/twiml/*` and
      `/twilio/*`
- [x] Canonical callback URL reconstruction from `PUBLIC_BASE_URL` —
      attacker-supplied `X-Forwarded-Host`/`X-Forwarded-Proto` cannot alter
      validation; nginx overwrites forwarded headers at the edge
- [x] Form-encoded and JSON/`bodySHA256` validation per Twilio's documented
      schemes
- [x] Facebook webhook signature enforcement (fails closed when
      `FACEBOOK_APP_SECRET` is unset)
- [x] Bearer API authentication between `ai-agent` and `eastwood-core`
      (`EASTWOOD_API_KEY`; constant-time compare; 401 absent, 403 invalid;
      fail closed when unconfigured)
- [x] Production boot guards: refuses to start without the API key or with
      signature validation disabled
- [x] Tenant ID pinning — no client-controlled tenant parameters remain
- [x] Restricted nginx exposure for `ai-agent` — only the Twilio media
      WebSocket is public; session admin, health, and metrics stay internal
- [x] `/twilio/` nginx proxy routing (status callbacks previously never routed
      through nginx at all)
- [x] FastAPI docs/OpenAPI endpoints disabled
- [x] Safe security logging — route, result, reason code, request id only;
      never secrets, signatures, bodies, or phone numbers

Verified against live Docker stack:
- eastwood-core: 182 passed
- ai-agent: 72 passed
- TypeScript: clean
- Canonical Twilio signature: 200
- Spoofed-origin signature: 403
- Unsigned nginx Twilio request: 403
- Media WebSocket edge handshake: 101
- Internal ai-agent HTTP endpoints at edge: 404

### Remaining operator steps

- [ ] Replace the temporary ngrok URL with a **stable HTTPS `PUBLIC_BASE_URL`**.
      Twilio TwiML, status, and media-stream callbacks are built from this; when it
      rotates, every webhook 404s mid-campaign. Signature validation reconstructs
      the signed URL from it, so it must be exactly what Twilio is given.
      *Production now refuses to boot with an empty, non-HTTPS, local, or
      temporary-tunnel URL (ngrok, trycloudflare, localtunnel). The overlay binds
      nginx to `127.0.0.1:18000`; put a TLS proxy or a named tunnel in front.*
- [x] Set **`EASTWOOD_API_KEY`** to a long random secret (`openssl rand -hex 32`)
      — one value shared by `eastwood-core` and `ai-agent` (compose passes it to
      both). Production refuses to boot without it. *Generated into the local
      `.env.production` (2026-11-24).*
- [x] Keep **`TWILIO_SIGNATURE_VALIDATION_ENABLED=true`** (the default).
      Production refuses to boot with it off. *Pinned by the production overlay.*
- [ ] Point the Twilio number's **"A message comes in"** webhook at
      `POST {PUBLIC_BASE_URL}/twilio/sms`. Without it a texted STOP only stops
      texts (Twilio's own opt-out) and the prospect stays dialable; with it the
      number is suppressed for calls and texts and everything pending is
      cancelled. *Endpoint verified with simulated signed requests.*
- [ ] Verify the Twilio **voice / status / media** routes resolve:
  - `POST|GET {PUBLIC_BASE_URL}/twiml/call/{session_id}` (TwiML)
  - `POST {PUBLIC_BASE_URL}/twilio/status/{session_id}` (terminal status → retry +
    session close + slot release)
  - `wss://…/agent/sessions/{session_id}/media` (media stream via nginx `/agent/`)

  *All three verified through nginx with simulated, signed Twilio requests
  (2026-11-24); re-verify against the real `PUBLIC_BASE_URL`.*
- [ ] Verify **TLS** on `PUBLIC_BASE_URL` (valid cert, HTTPS only).
- [x] Verify service health on the **internal** network (`GET /health` on both
      services via their compose ports) — nginx intentionally does not proxy
      health or metrics to the public edge. *Verified: both 200 internally; at
      the edge `/health` and `/metrics` reach only the static site, and
      `/agent/health`, `/agent/metrics`, `/agent/sessions/*` return 404.*
- [ ] Run a **full webhook smoke test**: place one call to your own phone, answer,
      let it complete, and confirm the status callback fired (slot released,
      session closed) — with signature validation enabled throughout. *The
      simulated version passed end to end; the live call needs rotated Twilio
      credentials and the stable URL.*

---

## 6. Agent notification and handoff

- [ ] Set `AGENT_NOTIFICATION_PHONE_E164` (the licensed agent's number).
- [ ] Book a **test appointment** end to end.
- [ ] Confirm **exactly one** SMS arrives (booking is idempotent at both ends).
- [ ] Confirm the SMS contains:
  - [ ] prospect name
  - [ ] phone
  - [ ] appointment time
  - [ ] purpose
  - [ ] beneficiary
  - [ ] budget / coverage target
  - [ ] health summary
  - [ ] quote
  - [ ] motivation
  - [ ] objections
  - [ ] open questions
- [ ] Confirm the **full handoff profile is persisted** in `handoff_profiles`
      (the canonical record; the SMS is only the alert).
- [x] Confirm that with `AGENT_NOTIFICATION_PHONE_E164` **unset**, a booking still
      succeeds (the brief is skipped with a warning, profile still persisted) — do
      not launch with it unset, but know the failure mode is soft. *Verified
      2026-11-24 (booking 201, `handoff_profiles` row, warning logged; same soft
      path for the call-outcome SMS). The production overlay now requires the
      variable, so production cannot start with it unset.*

---

## 7. Controlled test campaign

Outbound is a two-key launch. Nothing prospect-facing — dial, follow-up SMS or
callback, consent request, booking confirmation — leaves the system until both:

1. `OUTBOUND_ENABLED=true` in `.env.production` (deployment key; default `false`), and
2. an operator authorizes the campaign (runtime key, recorded with name and reason):
   `curl -X POST {core}/api/outbound/resume -H "Authorization: Bearer $EASTWOOD_API_KEY" -H 'Content-Type: application/json' -d '{"operator":"…","reason":"…"}'`

**Emergency stop:** `POST /api/outbound/halt` with the same body. The dialer stops
within 3 s and the follow-up worker within 30 s; connected calls finish
normally. `GET /api/outbound/status` shows the state. A fresh deployment has
never been authorized and starts halted; a halt survives restarts.

- [ ] Use a **tiny, operator-approved list** first — **5 to 10 numbers**,
      preferably numbers you control or have explicit consent to call.
- [ ] Verify **every number is `clear` or `exempt`** before dialing (query
      `leads.national_dnc_status`).
- [ ] `MAX_CONCURRENT_CALLS=1`.
- [ ] Conservative retry settings (leave the default ladder; consider lowering
      `DIAL_MAX_ATTEMPTS` for the first run).
- [ ] **Monitor every call live.**
- [ ] Practise the **emergency halt** once before the first dial and confirm
      `eastwood_outbound_permitted` drops to 0.
- [ ] Confirm, for the run *(each item below passed in the 2026-11-24 simulation;
      confirm again on the live run)*:
  - [ ] **slot release** — `dial_queue` rows leave `dialing` immediately on
        terminal status
  - [ ] **session cleanup** — `agent_sessions_active` returns to 0
  - [ ] **retry scheduling** — a no-answer reschedules once, at the right offset
  - [ ] **no duplicate SMS** — one agent brief per booking
  - [ ] **no duplicate `call_ended`** — one outcome per connected call, none for
        unanswered calls
  - [ ] **quote persistence** — `quote_records` written with honesty labels
  - [ ] **appointment persistence** — `appointments` + `handoff_profiles` written
  - [ ] **DNC handling** — a mid-call "stop calling" suppresses the number and
        cancels pending redials
  - [ ] **suppression on re-import** — re-importing a suppressed number does not
        re-queue it

---

## 8. Metrics and stop conditions

Prometheus metric names in `code font`; where no dedicated metric exists, the
observation source is noted.

Track:
- `agent_sessions_active` — active sessions (must idle at 0)
- active dial slots — DB: `count(dial_queue where status='dialing')`
- attempts by lead — DB: `dial_queue.attempt_count`
- no-answer / busy / failed rates — DB: `dial_queue.status` distribution
- connected calls — `eastwood_agent_events_received_total{event_type="call_ended"}`
- quotes presented — `eastwood_agent_events_received_total{event_type="quote_presented"}`
- appointments booked — `eastwood_agent_events_received_total{event_type="appointment_booked"}`
- DNC requests — `eastwood_agent_events_received_total{event_type="dnc_request"}`
- suppressions — DB: `count(suppressions)`
- retries scheduled — logs (`domain.dialing`) / `dial_queue.scheduled_at` deltas
- abandoned sessions reaped — `agent_sessions_reaped_total`
- duplicate terminal events — `agent_duplicate_terminal_events_total`
- SMS notification failures — logs (`domain.notifications`); no metric yet
- tool usage / vetoes — `agent_tool_invocations_total`, `agent_policy_decisions_total`
- campaign gate — `eastwood_outbound_permitted` (1 = authorized, 0 = halted/disarmed)
- gated contact decisions — `eastwood_outbound_contact_decisions_total{channel,result}`
  (`sent`, `queued`, `blocked`, `deferred`, `skipped_no_provider`)

Grafana → **Eastwood → Eastwood operations** charts these.

**Immediate stop conditions** — halt with `POST /api/outbound/halt`:
- any DNC request followed by further dialing or SMS to that number
- an `unknown` or `blocked` lead reaches Twilio
- a duplicate appointment notification
- `agent_sessions_active` or active dial slots fail to return to zero
- a quote presented without the required discovery profile
- the agent names a carrier on a formula estimate
- webhook failures exceed a defined threshold (set one, e.g. >2% of callbacks)
- an unexpected cost spike (OpenAI realtime minutes or Twilio spend)

---

## 9. Launch record

Record for each campaign:
- commit SHA (`git rev-parse HEAD`)
- deployment date / time
- operator
- environment (`APP_ENV`, host)
- Twilio number
- lead source (file / vendor)
- campaign size
- area-code scope (`DNC_AREA_CODES`)
- DNC dataset `downloaded_at` + a file checksum from the manifest
- quote mode (A or B)
- config snapshot **excluding secrets** (`env | grep -vE 'KEY|TOKEN|PASSWORD|SAN'`)
- known limitations at time of launch

---

## 10. Final go/no-go

The system is **GO only when every required item above is checked.**

### Current known blockers

These must be resolved before the first live dial. None is application code — all
are configuration, credentials, data, or third-party registration (updated
2026-11-24; costs in [`launch-dependencies-and-costs.md`](launch-dependencies-and-costs.md)):

- [ ] **Real FTC registry credentials and a verified download endpoint** — the
      provider is built and tested, but `local_csv` dials nobody and no real
      `DNC_REGISTRY_DOWNLOAD_URL` / SAN is configured yet (§2).
- [ ] **A fresh, complete, in-scope DNC dataset** — `scripts/refresh_dnc.py` has
      not been run against the real registry; no lead is currently `clear` (§2).
- [ ] **Rotated Twilio and OpenAI credentials** — confirmed 2026-11-24 that the
      configured values are the ones exposed before the history scrub; they
      still exist in the two pre-scrub bundles (§1).
- [ ] **Stable `PUBLIC_BASE_URL`** — currently a temporary ngrok tunnel (§5).
      Production now refuses to boot with a tunnel URL.
- [ ] **A2P 10DLC registration** for the Twilio number (brand + campaign).
      Eastwood sends SMS (follow-ups, consent requests, agent briefs); carriers
      block unregistered 10DLC traffic.
- [ ] **Twilio inbound SMS webhook** → `{PUBLIC_BASE_URL}/twilio/sms` (§5).
- [ ] **An explicitly chosen quote mode** — default today is estimate-and-book via
      the formula adapter; carrier rate tables are not loaded (§4).
- [x] **Production-safe environment values** — now enforced rather than
      remembered: the production overlay pins `APP_ENV=production`,
      `BYPASS_TCPA=false`, an empty `DNC_TEST_ALLOWLIST`,
      `RESET_RATE_TABLE_ON_BOOT=false` and signature validation, and requires
      every credential; the boot guard refuses thresholds below 75/50, an empty
      or invalid `ACTIVE_STATES`, and a non-FTC DNC provider (all verified in
      the real image, 2026-11-24).
- [ ] **Fill `.env.production`** — external credentials, `PUBLIC_BASE_URL`,
      `ACTIVE_STATES`, the agent's number; leave `OUTBOUND_ENABLED=false` until
      the live smoke test.
- [ ] **Regulatory review** — the items in
      [`launch-dependencies-and-costs.md`](launch-dependencies-and-costs.md#regulatory-prerequisites)
      (AI-voice consent wording, opt-out and recording disclosures, licensing,
      state rules) signed off by counsel.

When every box above and in §1–§9 is checked: **GO.**
