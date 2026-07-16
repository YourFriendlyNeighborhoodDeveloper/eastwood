# Eastwood Go-Live Checklist

Operator-facing steps to take Eastwood from its current tested state to **one
controlled live campaign**. Work top to bottom. The system is GO only when every
required box is checked (§10).

Conventions:
- Environment variables live in the repo-root `.env` (loaded by `docker-compose`
  and by `eastwood-core` via pydantic-settings). `.env` is gitignored; start from
  `.env.example`.
- Schema is applied automatically when `eastwood-core` boots (idempotent
  `create_all` + `ALTER` in `app/main.py`); the SQL under `migrations/postgres/`
  is the record, not a manual step.
- Prometheus scrapes `eastwood-core:/metrics` and `ai-agent:/metrics`; Grafana is
  in `docker-compose.yml`.

---

## 1. Secrets and account safety

- [ ] Rotate the **OpenAI API key** (issue a new key, then revoke the old one).
- [ ] Rotate the **Twilio auth token** (Twilio console → rotate; update any other
      consumers first).
- [ ] Update local `.env` with the new `OPENAI_API_KEY` and `TWILIO_AUTH_TOKEN`
      (and the `EASTWOOD_TWILIO_*` mirrors).
- [ ] Verify the **old keys are disabled** (an API call with the old key must fail).
- [ ] Confirm **no secrets appear in currently tracked files**:
      `git grep -nE 'sk-(proj-)?[A-Za-z0-9]|AC[0-9a-f]{32}' -- . ':!.env.example'`
      should return nothing.
- [ ] **Known limitation — git history still contains prior secrets.** The earlier
      `.env` (with a live Twilio token and OpenAI key) is in commit `e84ad7e`.
      Untracking it did not remove it from history. If this repo has ever been
      pushed or shared, treat those keys as burned even after rotation. History
      rewrite (`git filter-repo`) is the only removal; it is out of scope for this
      checklist but must be tracked.

---

## 2. National DNC readiness

The dialer will **not** call a lead unless `national_dnc_status` is `clear` or
`exempt`. New leads default to `unknown`, which is not dialable. With the default
`local_csv` provider nothing ever resolves to `clear`, so **you must configure the
official provider before any real dial.**

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

- [ ] `APP_ENV=production`
- [ ] `BYPASS_TCPA=false`
      — With `APP_ENV=production` **and** `BYPASS_TCPA=true`, `eastwood-core`
      refuses to boot (compliance guard). A clean boot confirms this is off.
- [ ] `DNC_TEST_ALLOWLIST` empty
      — The dev test allowlist scrubs self-owned numbers as `exempt` without a
      provider. In production it must be empty; `eastwood-core` refuses to boot
      with `APP_ENV=production` and a non-empty allowlist (same compliance guard).
- [ ] `SCORE_HOT_THRESHOLD=75`
- [ ] `SCORE_WARM_THRESHOLD=50`
- [ ] **Verify state timezone calling windows** — dialing is gated to 8am–9pm local
      time per the lead's state (`domain/tcpa.py`). Spot-check a lead in each
      campaign state.
- [ ] **Verify entity-specific suppression checks** — a prior "stop calling"
      request (the `suppressions` table) blocks both dialing and re-import.
- [ ] **Verify national DNC checks** — see §2; `unknown`/`blocked` never dial.
- [ ] **Verify callable phone normalization** — every lead has a valid E.164
      `phone_e164`; rows without one were dropped at ingest.
- [ ] **Verify max concurrency and retry limits**:
  - `MAX_CONCURRENT_CALLS` (hard cap on simultaneous calls)
  - `DIAL_MAX_ATTEMPTS` (attempts per lead)
  - retry ladder `_RETRY_OFFSETS_HOURS = [4, 24, 32, 56, 80]` (`domain/dialing`)
- [ ] **Verify `ACTIVE_STATES` includes every campaign state.** Auto-dial routing
      depends on scoring; a lead outside `ACTIVE_STATES` loses its state-fit points
      and may route to `nurture` instead of `auto_dial`. This is the most common
      reason "nothing dials" after import.
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

### Mode A — estimate-and-book (recommended for first launch)
- [ ] `RESET_RATE_TABLE_ON_BOOT=false` (does not truncate rate tables on boot, but
      with no rate rows loaded the formula adapter is still used)
- [ ] `FormulaAdapter` output remains labeled `quote_type=estimated`
- [ ] `carrier_rate=false`
- [ ] `bindable=false`
- [ ] Carrier name stays **hidden from the prospect** (the tool returns `carrier:
      null` for estimates)
- [ ] Imogen uses the approved estimate language: *"Based on what you told me, we
      may be looking around $X per month. I'd like to book a licensed review to
      confirm the exact carrier and final rate."*

### Mode B — carrier-rate mode
- [ ] Load validated carrier rate tables (`carrier_rate_tables`) with
      `RESET_RATE_TABLE_ON_BOOT=false`
- [ ] Verify each row's `source` / `version` / `effective_date`
- [ ] Confirm quote labels show `rate_source=rate_table` (or `cms_api`) and
      `carrier_rate=true`
- [ ] `bindable` remains `false` (Eastwood submits no application; nothing binds)
- [ ] Run a sample comparison against carrier quoting portals for a spread of
      ages/face amounts before trusting the numbers on a call

> Whichever mode: no quote Eastwood produces is bindable. The appointment with a
> licensed agent is the close.

---

## 5. Public webhook infrastructure

### Completed — webhook authentication and API auth (commit `5cdda6c`)

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
- [ ] Set **`EASTWOOD_API_KEY`** to a long random secret (`openssl rand -hex 32`)
      — one value shared by `eastwood-core` and `ai-agent` (compose passes it to
      both). Production refuses to boot without it.
- [ ] Keep **`TWILIO_SIGNATURE_VALIDATION_ENABLED=true`** (the default).
      Production refuses to boot with it off.
- [ ] Verify the Twilio **voice / status / media** routes resolve:
  - `POST|GET {PUBLIC_BASE_URL}/twiml/call/{session_id}` (TwiML)
  - `POST {PUBLIC_BASE_URL}/twilio/status/{session_id}` (terminal status → retry +
    session close + slot release)
  - `wss://…/agent/sessions/{session_id}/media` (media stream via nginx `/agent/`)
- [ ] Verify **TLS** on `PUBLIC_BASE_URL` (valid cert, HTTPS only).
- [ ] Verify service health on the **internal** network (`GET /health` on both
      services via their compose ports) — nginx intentionally does not proxy
      health or metrics to the public edge.
- [ ] Run a **full webhook smoke test**: place one call to your own phone, answer,
      let it complete, and confirm the status callback fired (slot released,
      session closed) — with signature validation enabled throughout.

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
- [ ] Confirm that with `AGENT_NOTIFICATION_PHONE_E164` **unset**, a booking still
      succeeds (the brief is skipped with a warning, profile still persisted) — do
      not launch with it unset, but know the failure mode is soft.

---

## 7. Controlled test campaign

- [ ] Use a **tiny, operator-approved list** first — **5 to 10 numbers**,
      preferably numbers you control or have explicit consent to call.
- [ ] Verify **every number is `clear` or `exempt`** before dialing (query
      `leads.national_dnc_status`).
- [ ] `MAX_CONCURRENT_CALLS=1`.
- [ ] Conservative retry settings (leave the default ladder; consider lowering
      `DIAL_MAX_ATTEMPTS` for the first run).
- [ ] **Monitor every call live.**
- [ ] Confirm, for the run:
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

**Immediate stop conditions** (halt the campaign):
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
are configuration, credentials, or data:

- [ ] **Real FTC registry credentials and a verified download endpoint** — the
      provider is built and tested, but `local_csv` dials nobody and no real
      `DNC_REGISTRY_DOWNLOAD_URL` / SAN is configured yet (§2).
- [ ] **A fresh, complete, in-scope DNC dataset** — `scripts/refresh_dnc.py` has
      not been run against the real registry; no lead is currently `clear` (§2).
- [ ] **Rotated Twilio and OpenAI credentials** — prior keys are still valid and
      remain in git history (§1).
- [ ] **Stable `PUBLIC_BASE_URL`** — currently a temporary ngrok tunnel (§5).
      Webhook signature validation and API authentication are implemented and
      verified (commit `5cdda6c`); the URL itself is the remaining gap.
- [ ] **An explicitly chosen quote mode** — default today is estimate-and-book via
      the formula adapter; carrier rate tables are not loaded (§4).
- [ ] **Production-safe environment values** — confirm `APP_ENV=production`,
      `BYPASS_TCPA=false`, `DNC_TEST_ALLOWLIST` empty, thresholds `75`/`50`,
      `RESET_RATE_TABLE_ON_BOOT=false`, `ACTIVE_STATES` covering the campaign
      (§3), `EASTWOOD_API_KEY` set, and `TWILIO_SIGNATURE_VALIDATION_ENABLED=true`
      (§5 — the boot guard enforces the last two). `DNC_TEST_ALLOWLIST` scrubs listed numbers as exempt without a
      provider — a testing affordance only; `eastwood-core` refuses to boot in
      production if it is non-empty.

When every box above and in §1–§9 is checked: **GO.**
