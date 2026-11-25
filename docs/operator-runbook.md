# Operator runbook: from nothing to the first live transaction

The one sequence to follow to deploy Eastwood in production. It runs from the
first account to the milestone: **one authorized test lead texted, quoted from
a verified carrier rate sheet, and booked**. Then it covers the first real
campaign and daily operation.

Every step is configuration, paperwork or money, not code. The detail lives in
the linked documents; this page is the order to do things in.

- Costs and regulatory detail: [launch-dependencies-and-costs.md](launch-dependencies-and-costs.md)
- Carrier rates: [carrier-quoting-matrix.md](carrier-quoting-matrix.md)
- Detailed gates: [go-live-checklist.md](go-live-checklist.md)

Throughout, `ewc` is shorthand for the production compose command, run from the
repository root on the server:

```bash
alias ewc='docker compose -p eastwood-prod --env-file .env.production -f docker-compose.yml -f docker-compose.production.yml'
```

---

## 1. Accounts and paperwork (start first: approvals take days to weeks)

| # | What | Why | Cost |
| --- | --- | --- | --- |
| 1.1 | **Carrier appointments** for each carrier you will quote, plus agent-portal logins (through your agency or IMO) | Rate books and the carrier quote tool that verification compares against | $0 (state appointment fees vary) |
| 1.2 | **FTC National Do Not Call Registry**: Subscription Account Number (SAN), login, and the area codes your leads use | Production refuses to start without it; numbers in unsubscribed area codes are never contacted | First 5 area codes free, then $85 each per year |
| 1.3 | **Twilio**: rotate the account credentials (the old ones were exposed); buy one local number | Sending number and webhooks | Number $1.15/mo |
| 1.4 | **A2P 10DLC**: standard brand plus one campaign. Sample messages: the opener and nudges in `eastwood-core/domain/sms/playbook.py`. Opt-in description: your consent wording. | Carriers block unregistered texting; your brand's Trust Score sets the T-Mobile daily cap | Brand ≈ $46 one-time; campaign vetting $15 one-time; campaign ≈ $10/mo |
| 1.5 | **OpenAI key** (rotate) | Required by the production overlay for the voice service; SMS uses it only if you turn the reading model on | Usage only |
| 1.6 | **Counsel sign-off**: the consent names your agency and covers texts and AI; AI disclosure; state rules | Before any real prospect is contacted | Fixed-fee quote |

## 2. Server

1. **Provision.** A VPS with 2 vCPU, 4 GB RAM and 40 GB disk, plus Docker. Clone the repository (branch `production-hardening`, or `main` once merged).
2. **Env file.** `cp .env.production.example .env.production && chmod 600 .env.production`. Compose stops until every required value is set:
   - **Twilio and public URL:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`, `PUBLIC_BASE_URL`
   - **Do Not Call registry:** `DNC_SAN`, `DNC_REGISTRY_USERNAME`, `DNC_REGISTRY_PASSWORD`, `DNC_REGISTRY_DOWNLOAD_URL`, `DNC_AREA_CODES`
   - **Agency, consent and agent:** `CONSENT_AUTHORIZED_SELLERS` and `AGENCY_DISPLAY_NAME` (your agency exactly as the consent names it), `AGENT_NOTIFICATION_PHONE_E164`, `ACTIVE_STATES`
   - **Spend:** `OUTBOUND_DAILY_SPEND_LIMIT_USD`
   - **Secrets:** `EASTWOOD_API_KEY` (`openssl rand -hex 32`), `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`, `OPENAI_API_KEY`

   Also set `AGENT_AVAILABILITY` (your real hours, e.g. `mon-fri 17:00-20:00; sat 09:00-12:00`) and `APPOINTMENT_MAX_PER_DAY`. Leave `OUTBOUND_ENABLED=false`.
3. **HTTPS.** Put a named tunnel or TLS proxy in front of nginx on `127.0.0.1:18000`. `PUBLIC_BASE_URL` is its stable `https://` address; production refuses temporary tunnel URLs.
4. **Start.** `ewc up -d --build`, then `ewc ps` and `curl -s 127.0.0.1:18080/health`. The boot guard refuses an unsafe configuration and names the setting.
5. **Twilio console.** Set the number's "A message comes in" webhook to `PUBLIC_BASE_URL/twilio/sms` (POST). Delivery receipts are requested per message automatically. For voice, also set the URLs in [go-live-checklist.md §5](go-live-checklist.md).
6. **Do Not Call data.** Run `ewc exec eastwood-core python scripts/refresh_dnc.py`. Repeat at least every 31 days; older data can no longer clear a number. A monthly cron job is the simplest way.
7. **Backups (not built into the stack).** Run a nightly `ewc exec -T postgres pg_dumpall -U eastwood | gzip > /backups/eastwood-$(date +%F).sql.gz`, and copy the file off the server. It covers both databases: core and the voice agent.
8. **Dashboard.** From your laptop, run `ssh -L 18080:127.0.0.1:18080 <server>`, then open `http://127.0.0.1:18080/ops` and enter `EASTWOOD_API_KEY`. nginx never exposes it.

## 3. Carrier rates (once per plan; again whenever the carrier changes rates)

Rate books go in `data/private/imports/` on the server. That folder is gitignored and appears read-only in the container at `/app/imports/`. Work in the private volume:

```bash
ewc exec eastwood-core python -m scripts.rates extract /app/imports/<book>.pdf --unit <unit> --out /app/data/private/rates/<plan>
ewc cp eastwood-core:/app/data/private/rates/<plan>/meta.template.json ./meta.json   # fill every null from the rate book
ewc cp ./meta.json eastwood-core:/app/data/private/rates/<plan>/meta.json
ewc exec eastwood-core python -m scripts.rates checks-template /app/data/private/rates/<plan>/rates.csv --unit <unit> --out /app/data/private/rates/<plan>/checks.csv
ewc cp eastwood-core:/app/data/private/rates/<plan>/checks.csv ./checks.csv          # price each case in the carrier's quote tool
ewc cp ./checks.csv eastwood-core:/app/data/private/rates/<plan>/checks.csv
ewc exec eastwood-core python -m scripts.rates import /app/data/private/rates/<plan>/rates.csv --meta /app/data/private/rates/<plan>/meta.json
ewc exec eastwood-core python -m scripts.rates verify <sheet_id> --checks /app/data/private/rates/<plan>/checks.csv --verified-by "<you>"
ewc exec eastwood-core python -m scripts.rates list
```

- **The unit.** `--unit` describes what the book's numbers are (see [the matrix](carrier-quoting-matrix.md#how-a-rate-book-becomes-quotes)). Read `report.txt` before filling the meta.
- **Verification.** It passes only when every check matches the carrier's quote tool to the cent and every rate class has a check. Until then the sheet quotes no one.
- **Tenants.** Rate sheets belong to a tenant (`DEFAULT_TENANT_ID`). Section 4 runs in its own tenant, so you import and verify there first, then again in the production tenant. The prepared files are reused.

## 4. The first live transaction (the milestone)

Run this before any real lead is imported. It uses a separate tenant, so nothing in it can touch a real prospect. The script and its guards are in [controlled-live-sms-test.md](controlled-live-sms-test.md).

1. **Test tenant.** In `.env.production`, set `DEFAULT_TENANT_ID=live-test-1` and `OUTBOUND_ENABLED=true` (the test window only), then run `ewc up -d`.
2. **Rate sheet.** Import and verify the rate sheet for the plan you'll quote, as in section 3, with this tenant active.
3. **The recipient.** Use your own phone. It must clear the Do Not Call scrub: a number on the national registry is blocked in production, by design.
4. **Dry run.** Run it:

   ```bash
   ewc exec -e LIVE_TEST_RECIPIENT=+1XXXXXXXXXX eastwood-core python -m scripts.live_sms_test \
       --to +1XXXXXXXXXX --first-name <you> --last-name <you> --state MI --dob 1958-04-02 \
       --operator "<you>" --dry-run
   ```

   It refuses, with reasons, unless all of these hold:
   - Twilio is live and the URL is HTTPS
   - the tenant is empty
   - a **verified, non-test rate sheet prices your age and state**
   - the calendar has an open slot
5. **Run it.** Run the same command without `--dry-run` and type the last 4 digits. Reply as a prospect would: yes, coverage amount, age, tobacco, sex, health answers, budget, then pick an option and a time.
6. **Check** that:
   - the opener names your agency, says it's an AI assistant and gives STOP
   - every premium texted appears in `/ops → Conversations → Quotes` with its `Q…` reference, and equals the carrier quote tool for your facts
   - the appointment is in `/ops → Appointments` with the full profile, and the agent got the SMS alert
   - every text shows `delivered`
   - STOP ends it, and the confirmation is the last text
7. **Close the test window.** The script halts outbound and completes the campaign when it ends. Set `OUTBOUND_ENABLED=false` and `DEFAULT_TENANT_ID=default` (or your tenant name), run `ewc up -d`, and import and verify the rate sheets in that tenant. Record the result in [go-live-checklist.md §7](go-live-checklist.md).

## 5. First real campaign

1. **Upload.** In `/ops → Upload`, upload the lead file (CSV, XLSX or a PDF with text). Review the column preview and the rows that need attention, then import with the **lead-use authorization reference**. Without it every lead is quarantined. The Do Not Call scrub and qualification run on their own.
2. **Inventory.** Check `GET /api/inventory`: how many leads are textable, how many are quarantined and why, and which area codes still need a registry subscription.
3. **Campaign.** In `/ops → Campaigns`, create a small campaign:
   - **Budget:** a $25–50 budget and a daily cap
   - **Rate:** texts per minute well under your 10DLC throughput
   - **Size:** "Enqueue leads" with a small number, e.g. 50
4. **Launch.** Click **Launch**, then **Authorize outbound** (two keys: `OUTBOUND_ENABLED=true` and the authorization). **HALT** stops everything within seconds.
5. **Watch** the first replies live in `/ops → Conversations`. Scale only after the reply, quote and booking numbers look right. The calendar paces everything: when it's full, new outreach waits.

## 6. Daily and periodic operation

| When | What |
| --- | --- |
| Daily | `/ops` overview (texts, replies, quotes, bookings, spend, cost per appointment); record each appointment's outcome (attended / no-show / sold with premium and commission) |
| Every ≤ 31 days | `scripts/refresh_dnc.py` (registry freshness) |
| Each carrier rate change | Import the new rate book, verify it, `retire` the old sheet. Expired sheets stop quoting by themselves. |
| Yearly | Renew the FTC registry subscription and the area codes |
| Any problem | **HALT** in `/ops` (or `POST /api/outbound/halt`). It survives restarts until you authorize again. |
| Nightly | Backup, as in section 2 step 7 |
