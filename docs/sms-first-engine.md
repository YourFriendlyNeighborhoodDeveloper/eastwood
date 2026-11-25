# SMS-first lead-to-appointment engine

Eastwood's default way to work a lead book is now **text first**. Imogen texts
each authorized lead, holds a two-way conversation, collects what a quote
needs, quotes **only from a verified carrier rate sheet**, books the prospect
on the licensed agent's calendar, confirms, and reminds. Voice is optional
(`escalate_to_voice` per campaign), never the default first touch.

The operator's loop is four steps, all in the dashboard at
`http://127.0.0.1:18080/ops` on the server (reach it through an SSH tunnel; nginx
does not expose it):

1. **Upload leads.** CSV, XLSX or PDF (scanned PDFs are read by OCR). Preview
   how each column will be read, fix any mapping, import.
2. **Configure a campaign.** States, products, budget, texts per minute.
3. **Launch.** Enqueue, activate, and authorize outbound (the second key).
4. **Receive appointments.** Each one arrives with the full prospect profile.

## How a conversation runs

```
pending ─opener─▶ contacted ─reply─▶ active ─answers─▶ quoted ─pick─▶ booking ─▶ booked
   │                 │ no reply: 2 nudges, then dormant                    │
   │                 └ decline / wrong number / STOP ─▶ declined / closed / opted_out
   └ calendar full: outreach waits            stuck or asks for a person ─▶ handoff
```

| Step | What Imogen does |
| --- | --- |
| Opener | Names the agency, says she is an AI assistant, says how to opt out. One segment whenever it fits. |
| Qualify | Asks only what the quote needs and the lead file lacks: coverage amount, age, tobacco, sex, the rate sheet's own health questions, a monthly budget. Reads "10k", "fifteen thousand", "I'm 67", "nope". |
| Quote | Up to three options from the verified rate sheet (need, recommended, most within budget), each stored with a quote reference. Labelled preliminary: "the final price is set by the application". |
| Objections | Price (offers the lowest stored option), spouse, already covered, trust, "not now" (snoozes 3 days), questions (who is this, how did you get my number, is this AI, exam, cost). |
| Book | Offers three open times on the agent's calendar, spread at least 90 minutes apart and not already offered to someone else in the last 15 minutes; books atomically; confirms; reminds 24 h and 2 h before; handles reschedule (R) and cancel (C). |
| Hand off | A request to talk to a person, three unclear answers to the same question, or three chosen times taken in a row hands the prospect to the agent (SMS alert to `AGENT_NOTIFICATION_PHONE_E164`, and the dashboard's Conversations tab). |
| Stop | STOP, "not interested" or "wrong number" ends the conversation, suppresses the number for calls and texts, and sends one confirmation at most. |

Every appointment carries a profile for the agent: name, phone, time,
coverage wanted, age, tobacco, health answers, all quote options with
references, the option chosen, the carrier, objections, unanswered questions,
a qualification summary, the last 40 messages, and a recommended next action.

## What keeps it safe

**The model never writes a text.** Every outbound text comes from reviewed
templates in `domain/sms/playbook.py`. Understanding is rules first
(`domain/sms/nlu.py`); an economical model (`SMS_LLM_PROVIDER=openai`,
`gpt-5-mini`) is consulted only for replies the rules cannot read, and only to
classify them. With the default `SMS_LLM_PROVIDER=none` there is no AI cost at all.

**Every text passes `domain/sms/policy.py` before it is sent.** It refuses:

- any dollar amount that is not the coverage or premium of a quote stored for that conversation
- approval guarantees ("guaranteed acceptance", "can't be denied")
- "Medicare is a government benefit" claims
- requests for an SSN or bank details
- vague premium ranges
- a first text without the agency name, the AI disclosure and STOP
- non-GSM characters, or more than three segments

**Then the send-time gates**, in order:

1. the outbound arm switch and operator authorization (two keys)
2. SMS consent for this lead, National DNC clearance, suppression and lead status
3. 8am–9pm in the lead's time zone
4. the daily and campaign budgets
5. the global and per-campaign rate limits
6. at most once per conversation step

Anything a gate holds is deferred, not dropped.

Cost and loop guards:

- **Conversation cap.** No conversation sends more than 30 texts (`SMS_MAX_OUTBOUND_PER_CONVERSATION`). At the cap Imogen stops and hands off.
- **Follow-up limit.** A lead who never replies gets the opener and two nudges, then goes dormant.
- **Calendar-full pause.** While the agent's calendar has no open slot, new outreach waits.

## Quoting: verified rates only

Before this work the quote engine had no verified carrier rates:

- The CMS Medicare adapter's dataset id is a placeholder.
- The "formula" adapter is a curve fitted to benchmarks.
- The rate-table adapter ignored sex and effective dates and had no loader.

None of these may price a prospect now.

**Rate sheets** (`scripts/rates.py`) are the only source of a prospect-facing premium.

- **Format.** A carrier's rate book is loaded as per-$1,000 annual rates by age, sex, tobacco and tier, plus the policy fee, modal factors and rounding rule, with its source document and effective/expiry dates.
- **Verification.** A sheet is `draft` until at least three spot checks, copied from the carrier's own quote tool, match to the cent. Only `verified` sheets in their effective window, in the prospect's state, within face limits, quote.
- **Storage.** Every option is stored (`quote_records`: quote id, `Q`-prefixed reference, source, rate sheet, conversation, communicated-at), and the text can only state stored figures.
- **Preliminary only.** Quotes are labelled preliminary, never bindable. The final premium comes from the carrier's application.
- **No silent fallback.** If no verified sheet covers a prospect, Imogen says she can't give an exact rate by text and offers the agent instead. She never estimates.

`/api/quote` (used by the voice agent) is verified-only by default too (`PROSPECT_QUOTES_REQUIRE_VERIFIED=true`).

**Test sheets.** `tests/fixtures/rate_sheets/make_test_sheet.py` writes a synthetic sheet, "Synthetic Test Life (NOT A REAL CARRIER)", flagged `is_test`. It quotes only with `ALLOW_TEST_RATE_SHEETS=true`, which production refuses to boot with, and every text it prices starts "TEST RATES, not a real carrier."

**Verified carrier rates in this repository: none.** Loading them is an operator step that needs the carrier rate books and appointment paperwork, or a licensed rate source such as Compulife (supports final expense) ([launch-dependencies-and-costs.md](launch-dependencies-and-costs.md)).

## Ingestion

| Format | How it is read |
| --- | --- |
| CSV / TSV | Delimiter sniffed, BOM-safe, streamed |
| XLSX | First sheet, streamed (read-only mode) |
| PDF with text | Ruled tables via pdfplumber; otherwise column-aligned text, placing each word under the header column it overlaps (tight columns never merge) |
| Scanned / photographed PDF | Rendered at 300 DPI and read by Tesseract **locally** (no lead data leaves the server). Each row carries its OCR confidence; rows under `OCR_MIN_CONFIDENCE` (85) are imported **quarantined** for review |

**Preview.** `POST /api/imports/upload` stores the file under its SHA-256 (private volume) and previews it:

- how each column will be read, with suggestions for unrecognized headers
- the first rows exactly as they will import
- the rows that need a person, plus duplicate and issue counts

Overrides re-preview without importing. Scanned PDFs preview their first three pages only (about 2 s per page); the import reads every page. Hashing, opening and OCR run in a worker thread, so a long scan never stalls webhooks or the SMS engine.

**Import.** Idempotent per file, resumable, with an error report. It is followed automatically by the National DNC scrub and qualification, so an upload is campaign-ready.

**Fixed while testing:**

- Product matching was by substring, so "Life Insurance" was read as final expense ("fe" is inside "life"). It now matches whole words.
- A "Gender" column now maps to the quoting field `sex`.

## Dashboard and APIs

`/ops` is a single static page and needs no build step. It calls these APIs with the API key the operator enters, which is kept in session storage.

| Purpose | Endpoint |
| --- | --- |
| Upload, preview, re-map, import | `POST /api/imports/upload`, `POST /api/imports/uploads/{id}/preview`, `POST /api/imports/uploads/{id}/import`, `GET /api/imports` |
| Campaigns | `POST /api/campaigns` (`channel_strategy`, `messages_per_minute`, `daily_message_cap`, `escalate_to_voice`), `POST /api/campaigns/{id}/enqueue`, `POST /api/campaigns/{id}/status` |
| Outbound keys | `GET /api/outbound/status`, `POST /api/outbound/resume`, `POST /api/outbound/halt` |
| Conversations | `GET /api/conversations?status=`, `GET /api/conversations/{id}` (messages + stored quotes) |
| Appointments | `GET /api/appointments?upcoming=` (with profiles), `POST /api/appointments/{id}/outcome` (`attended`, `no_show`, `sold` with premium and commission) |
| Metrics | `GET /api/dashboard/overview?campaign_id=` |

The overview reports:

- leads imported
- texts sent, segments, replies, active conversations
- qualified prospects and quotes communicated
- appointments booked, by outcome, and attended
- policies sold and annual premium written
- commissions, spend, cost per booked appointment, and profit

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `AGENCY_DISPLAY_NAME` | Eastwood Insurance | How texts name the agency. It must match how the consent names it (required in production). |
| `AGENT_DISPLAY_NAME` | our licensed agent | How texts refer to the agent |
| `SMS_LLM_PROVIDER` / `SMS_LLM_MODEL` | none / gpt-5-mini | Model reads only unclear replies |
| `SMS_FOLLOWUP_HOURS` | 48,120 | Nudges after an unanswered opener |
| `SMS_STALL_HOURS` | 24,72 | Nudges when a conversation goes quiet |
| `SMS_REMINDER_HOURS` | 24,2 | Appointment reminders |
| `SMS_REPLY_OUTSIDE_HOURS` | false | Answer inbound texts outside 8am–9pm |
| `SMS_MESSAGES_PER_MINUTE` | 60 | Global send rate; set to your 10DLC throughput |
| `SMS_MAX_OUTBOUND_PER_CONVERSATION` | 30 | Cost guard |
| `SMS_OFFER_HOLD_MINUTES` | 15 | Times offered to one prospect are held back from others |
| `PROSPECT_QUOTES_REQUIRE_VERIFIED` | true | Prospect quotes only from verified rate sheets |
| `ALLOW_TEST_RATE_SHEETS` | false | Synthetic sheets (refused in production) |
| `OCR_MIN_CONFIDENCE` | 85 | Scanned rows below this are quarantined |
| `COST_SMS_PER_SEGMENT_USD` / `COST_SMS_INBOUND_PER_SEGMENT_USD` | 0.013 / 0.010 | Budget accounting |

## Commercial acceptance test

See the results section below and [capacity-and-load-test.md](capacity-and-load-test.md).
The test runs against an isolated stack with a simulated provider. Scripted prospects answer
Imogen's texts through the real signed `/twilio/sms` webhook (`SIMULATED_SMS_PROSPECTS=true`,
refused in production). Each phone number maps to a persona:

| Persona | Share | Behavior |
| --- | ---: | --- |
| booker | 30 % | Answers everything and books |
| price objector | 8 % | "Too expensive", then takes the lowest option |
| spouse | 6 % | Wants to talk to a spouse first |
| questioner | 8 % | Asks "who is this?" first |
| incomplete | 5 % | Gives a vague coverage answer at first |
| rescheduler | 5 % | Books, then reschedules |
| interrupted | 5 % | Goes quiet mid-qualification, returns on the nudge |
| too old | 3 % | 89, outside the rate sheet |
| wants call | 3 % | Asks to be called |
| decliner | 8 % | "Not interested" |
| opt out | 4 % | STOP |
| silent | 15 % | Never replies |

The mix is a test design, not a conversion forecast.

## Acceptance results (2026-11-25)

**What was run.**
- **Stack:** an isolated stack with a simulated provider: no Twilio account and provider hostnames pinned to loopback.
- **Data:** synthetic leads on fictional 555-01xx numbers.
- **Rates:** the synthetic rate sheet, imported and verified through `scripts/rates.py` (all four spot checks matched to the cent).
- **Test-only settings:** quiet hours bypassed (the run started after 9pm ET); follow-up and nudge timers compressed to 36 s and 72 s.
- **Calendar:** daily 08:00–22:00, 15-minute slots, 56 a day, 14 days.

Every check below is computed from the database by `verify_acceptance.py`, not from logs.

**Upload to campaign** (through the dashboard's APIs):

| Step | Result |
| --- | --- |
| Upload + preview | CSV 2,040 rows (0.1 s), XLSX 300 rows (0.2 s), scanned PDF 2 pages read by OCR (3.2 s; 1 low-confidence row flagged) |
| Import | 2,340 leads; 20 in-file duplicates and 20 invalid phones in the error report |
| Quarantined | 395 non-US numbers (Canadian/Caribbean area codes), 164 without consent, 139 with consent naming another company, the 40 PDF rows (no consent column), 1 low-OCR row |
| Scrub + qualify (automatic) | 1,444 textable; 176 on the DNC registry; 106 in unsubscribed area codes (never textable) |
| Products | 1,976 final expense, 364 life (no rate sheet: quoted without a number) |

**Conversations** (1,444 leads, two waves; the second filled the calendar):

| Check | Result |
| --- | --- |
| Texts | 8,475 out (all delivery receipts processed), 7,606 in; 7.8 segments per conversation |
| Text rules | 0 non-GSM texts, 0 over 3 segments; 1,308 / 1,308 first texts name the agency, disclose AI and give STOP; openers fit 1 segment |
| Quotes | 1,860 stored and communicated across 778 conversations; **0 dollar amounts not from a stored quote; 0 premiums differing from the rate book recomputed independently; 0 life leads priced** |
| Bookings | **832 = calendar capacity; 0 double-booked; ≤ 56 a day; every booking confirmed; every profile complete**; 672 / 672 quoted bookings carry quote references; 49 / 49 appointments within 24 h reminded; 64 reschedules / cancellations released their slots |
| Calendar full | 109 prospects waitlisted (re-checked daily, no texts while full); 136 openers held; follow-ups kept running |
| Opt-out / decline | 50 STOP and 104 declines: 0 texts after a STOP, 0 beyond the one confirmation after a decline |
| Reply latency | p50 0.08 s, p95 1.5 s (max 491 s: the 16 confirmations of defect 19) |
| Cost (at $0.013 / $0.010 per segment) | $208.89 for 1,308 conversations: **$0.16 per conversation, $0.25 per booked appointment** |

The first wave alone (600 leads, before the calendar filled) produced:
- 435 booked, 88 dormant, 43 declined, 29 opted out and 5 handed off.
- $0.23 per booked appointment.

Outcome by persona (both waves):

| Persona | Outcome |
| --- | --- |
| booker | 353 booked, 47 waitlisted, 16 handed off |
| price objector | 88 booked, 14 waitlisted, 3 handed off |
| spouse | 63 booked, 8 waitlisted, 1 handed off |
| questioner | 70 booked, 6 waitlisted, 5 handed off |
| incomplete | 61 booked, 3 waitlisted, 1 handed off |
| rescheduler | 62 booked, 10 waitlisted |
| interrupted | 64 booked, 14 waitlisted, 4 handed off |
| too old (89) | 37 booked without a number, 5 waitlisted, 2 handed off |
| wants call | 34 booked, 2 waitlisted |
| decliner | 104 declined |
| opt out | 50 opted out |
| silent | 181 dormant after 3 texts |

**Not covered here.** These stay on the go-live checklist:
- real carrier delivery
- the real Twilio webhook signature
- real prospects' language
- the controlled live test ([controlled-live-sms-test.md](controlled-live-sms-test.md))

### Defects the acceptance runs found (all fixed and covered by tests)

| # | Defect | Fix |
| --- | --- | --- |
| 1 | **Booking stampede.** Every prospect choosing at the same moment was offered the same three earliest times; losers got the next same three. One conversation bounced 85 times on "that time was just taken". | Offered times are held back from other prospects for 15 minutes. Each conversation draws from the earliest open times in its own order, spread 90+ minutes apart. Three taken picks in a row hand off to the agent. Collision handoffs fell from 70 to 4–15 per 600 conversations. |
| 2 | Nothing capped a conversation's length, so any loop would keep spending. | 30-text cap per conversation (`SMS_MAX_OUTBOUND_PER_CONVERSATION`), then handoff. |
| 3 | "Reply 1, 2 or 3" shown when only two options or times existed; a "3" looped on "Sorry, I didn't catch that". | Prompts match the count ("Reply 1 or 2"). A third unclear answer to the same question hands off to the agent. |
| 4 | A prospect's text and the reply written in the same transaction shared one timestamp, so the transcript order (and the agent's profile) was ambiguous. | Message timestamps use statement time (`clock_timestamp()`). |
| 5 | The SMS engine's queue, its reminders, the dialer's queue and the legacy follow-ups read every tenant while the outbound gate checked only one. | All four are scoped to the tenant whose gate was checked. |
| 6 | "Life Insurance" was classified as final expense ("fe" occurs inside "life"), so life leads would be priced from final-expense rates. | Product keywords match whole words. |
| 7 | A file uploaded from the dashboard was never DNC-scrubbed or qualified, so it could never be texted. | Upload import runs the scrub and qualification afterwards. |
| 8 | The upload lookup could pick the file-name sidecar instead of the file; batches were named by hash. | Only lead-file extensions match; batches keep the original file name. |
| 9 | OCR of a scanned PDF ran on the event loop and would freeze webhooks and the engine for minutes. | Hashing, opening and OCR run in a worker thread; previews OCR only the first 3 pages. |
| 10 | Column-aligned PDF text merged adjacent columns when the gap was narrow. | Words are placed under the header column they overlap. |
| 11 | A waitlisted prospect would be re-sent "the calendar is full" every day and flipped back to active. | The daily re-check texts only when a time has actually opened. |
| 12 | While the calendar was full, held openers filled every batch and starved follow-ups, nudges and deferred replies. | Held outreach is left out of the batch. |
| 13 | Delivery receipts were never requested, so no text ever showed as delivered. | Each text asks for a receipt at `/twilio/sms-status/{id}`; receipts never step back ("sent" after "delivered"). |
| 14 | A STOP never appeared in the conversation transcript. | The opt-out text is recorded (idempotent) and its inbound cost booked. |
| 15 | Legacy follow-up texts said "this is Ryan", claimed a rate "won't last", gave a made-up "1-800-EASTWOOD" number, used em dashes (UCS-2, ~2x cost), could send `{quote_amount}` literally, and stated unverified premiums. | Rewritten (agency-named, GSM-7, no urgency), checked by the same text policy, priced only from a stored verified quote (in dollars, not cents), and blocked when any placeholder is unfilled. |
| 16 | The voice agent's quote fallback defaulted to $99.00 from "Eastwood Preferred". | No premium unless the engine returned one. |
| 17 | The opener took two segments. | One segment whenever it fits (all tested names and agencies). |
| 18 | The load-test harness built a new TLS context for every simulated reply and receipt, which took about 2/3 of core CPU. | One shared HTTP client. |
| 19 | A reply deferred by the rate limit after the prospect was already booked (the "You're booked" confirmation, 16 of 832 at the burst peak) was never sent: the scheduler read only active conversations. | Conversations holding a deferred reply are always picked up; every gate is re-checked at send. Restarting with the fix delivered all 16. |

