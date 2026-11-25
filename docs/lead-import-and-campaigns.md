# Importing a lead book and running a campaign

The operator path from a spreadsheet of leads to a budgeted, supervised
campaign. Nothing on this page contacts anyone until the very last step, and
that step needs two separate keys.

Commands run inside the core container. Put the file in `data/private/imports/`
on the host (gitignored); it appears read-only at `/app/imports/` in the
container. `$API` below is the core API (`http://127.0.0.1:18080` on the
production host) and every call needs `-H "Authorization: Bearer $EASTWOOD_API_KEY"`.

## 0. Before you import

- **Lead-use authorization.** Have the written document that lets you use these
  leads (an assignment or purchase agreement from their owner). Its reference
  goes on the import; without it every lead is quarantined.
- **Consent wording.** Set `CONSENT_AUTHORIZED_SELLERS` to your agency's name as
  consent disclosures state it. Consent that names another company — including
  a former employer — does **not** authorize your AI-voice calls or texts, and
  those leads are quarantined (reason `consent_other_seller`).

## 1. Import (CSV or XLSX)

```bash
docker compose exec eastwood-core python -m scripts.leads import /app/imports/book.xlsx \
  --source "Lead book from <owner>" --owner "<your agency>" --acquired 2026-11-20 \
  --use-authorization "<document reference>"
```

- Columns are mapped automatically (e.g. `Other Phone 1`, `Mobile`, `Date Created`,
  `TCPA Consent`, `TrustedForm Cert URL`, full state names). Override a header with
  `--map "Header Name=phone"` (fields: `phone`, `first_name`, `last_name`, `state`,
  `dob`, `acquired_at`, `product_line`, `lead_owner`, `external_id`, `consent_text`,
  `consent_captured_at`, `consent_url`, `consent_certificate`, `dnc_flag`, …).
- **Idempotent:** importing the same file again returns the original batch.
- **Resumable:** if the import stops for any reason, run the same command again;
  it continues after the last committed row.
- **Duplicates:** one lead per phone number. Repeats in the file, numbers already
  in Eastwood, and previously opted-out numbers are reported, not inserted.

Then:

```bash
python -m scripts.leads report <batch_id>            # counts, issue codes, quarantine reasons
python -m scripts.leads errors <batch_id> > errors.csv   # row number + code, no personal data
```

## 2. Inventory and registry cost

```bash
python -m scripts.leads inventory          # Markdown; --json for machine output
curl -H "Authorization: Bearer $KEY" "$API/api/inventory?format=md"
```

Breaks the book down by state, area code, product interest, lead age, consent
status and contact eligibility, and prices FTC registry access from the
**actual area codes**, with a coverage curve (how many leads the busiest 5, 10,
20 … area codes reach, and what each costs).

Eligibility categories:

| Category | Meaning |
| --- | --- |
| `ai_voice_and_sms` / `ai_voice_only` | Imogen may call (still subject to DNC, hours, campaign, budget) |
| `manual_review_only` | Lead use is authorized but consent does not cover AI voice — yours to review and, if lawful, call yourself |
| `no_contact` | Not usable (no lead-use authorization, DNC flag, non-US number, no state) |

## 3. National DNC scrub and qualification

```bash
python -m scripts.leads scrub      # against the downloaded FTC dataset
python -m scripts.leads qualify    # triage + score authorized leads (no third-party lookups)
```

Numbers in area codes you did not subscribe to stay `unknown` and are never dialed.

## 4. Campaign

```bash
# Create (budget is a hard ceiling; daily_budget_usd optional)
curl -X POST $API/api/campaigns -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "name": "MI final expense, December", "budget_usd": 250, "daily_budget_usd": 25,
  "max_concurrent_calls": 1, "calls_per_minute": 6, "states": ["MI"], "product_lines": ["final_expense"]}'

# Queue the leads it may call (authorized, DNC-cleared, in scope, not already queued)
curl -X POST $API/api/campaigns/<id>/enqueue -H "$AUTH" -d '{"limit": 300}'

# Activate it
curl -X POST $API/api/campaigns/<id>/status -H "$AUTH" -d '{"status": "active"}'
```

Pace the campaign to your calendar: every appointment needs one of your open
slots. `limit` on enqueue and `daily_budget_usd` are the two dials.

## 5. Arm and authorize (the only step that contacts anyone)

1. `OUTBOUND_ENABLED=true` in `.env.production`, then redeploy.
2. `POST /api/outbound/resume {"operator": "...", "reason": "..."}`.

**Emergency stop:** `POST /api/outbound/halt` — the dialer stops within 3 s,
follow-ups within 30 s.

Automatic stops:

| Trigger | Effect |
| --- | --- |
| Campaign spend reaches `budget_usd` | Outbound **halted** (operator `budget-guard`), campaign `budget_exhausted`; resume needs you |
| Day's spend reaches `OUTBOUND_DAILY_SPEND_LIMIT_USD` or the campaign's daily budget | No new calls until tomorrow (agent's timezone) |
| `DIAL_CALLS_PER_MINUTE` / campaign `calls_per_minute` | Dialing waits for the window |
| `MAX_CONCURRENT_CALLS` / campaign `max_concurrent_calls` | Dialing waits for a free line |
| Your calendar has no open slot (`PAUSE_DIALING_WHEN_CALENDAR_FULL`) | Dialing waits until a slot opens |
| A call reaches `MAX_CALL_MINUTES` (default 15) | Twilio ends it — this is what makes the budget ceilings hard limits |
| A status callback never arrives (crash, deploy) | The call is settled from Twilio's own record within ~2 minutes |

## 6. Your calendar

`AGENT_AVAILABILITY="mon-fri 17:00-20:00; sat 09:00-12:00"` with
`APPOINTMENT_MAX_PER_DAY`, `APPOINTMENT_SLOT_MINUTES`,
`APPOINTMENT_BUFFER_MINUTES`, `APPOINTMENT_MIN_NOTICE_HOURS`,
`AGENT_BLACKOUT_DATES`. Imogen offers only real open slots; a slot can be booked
once, and never past the daily cap.

## 7. Reporting

```bash
curl -H "$AUTH" "$API/api/reports/operations?since=2026-12-01T00:00:00Z&campaign_id=<id>"
curl -H "$AUTH" "$API/api/campaigns/<id>"
```

Attempts, answered calls, voicemails, qualified prospects (quoted),
appointments, opt-outs, conversion rates, estimated spend by kind, and cost per
answered call / qualified prospect / appointment. Spend is estimated from the
`COST_*` unit prices — reconcile monthly with the Twilio and OpenAI invoices and
adjust them.
