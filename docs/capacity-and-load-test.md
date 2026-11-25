# Capacity and load test — 27,000 leads (2026-11-24)

How Eastwood behaved with a 27,000-lead book and a sustained simulated
campaign, and what server it needs. Every number below was measured; none is
a projection.

## How it was tested

- **Data:** 27,810 synthetic rows (27,000 unique numbers) in CSV and XLSX —
  fictional 555-01xx numbers across 275 area codes, shaped like a CRM export
  (`Mobile` / `Other Phone 1`, full state names, `Date Created`, `TCPA Consent`).
  Mix: 55 % consent naming the importing agency, 25 % consent naming a
  *different* company (the former-employer case), 10 % no consent, 10 %
  undocumented consent, 3 % source DNC flags, plus 540 invalid phones and 270
  in-file duplicates.
- **Registry:** a synthetic FTC dataset (≈ 8 % of numbers listed, 20 of 275 area
  codes unsubscribed), loaded through the production `ftc_registry` provider.
- **Telephony:** `TELEPHONY_PROVIDER=simulated` — a fake Twilio inside the stack
  that delivers signed status callbacks through nginx and plays prospects
  (≈ 26 % reach a person; quotes, bookings and spoken opt-outs flow through
  Kafka and the real API). Nothing left the machine: no Twilio account SID,
  provider hostnames pinned to loopback.
- **Stack:** all nine services on Docker Desktop (7.65 GiB VM, Apple Silicon
  laptop), isolated compose project and database.

## Import

| Run | Rows | Time | Rows/s | Peak RSS |
| --- | ---: | ---: | ---: | ---: |
| CSV, clean database | 27,810 | 15.3 s | 1,816 | 93 MB |
| XLSX, clean database | 27,810 | 19.7 s | 1,410 | 100 MB |
| XLSX into a database already holding the same leads (all duplicates) | 27,810 | 10.3 s | 2,710 | 80 MB |
| Same CSV again (idempotent) | — | 0.06 s | no-op | 64 MB |

- **Crash test:** the import was killed with SIGKILL at row 18,001. Running the
  same command again resumed at row 18,001 and finished with exactly 27,000
  leads and 27,000 distinct numbers — nothing lost, nothing doubled.
- **Outcome (final code):** 27,000 inserted, 259 in-file duplicates, 551
  rejected (540 invalid phones, 11 repeats of numbers the same file had flagged
  DNC), 11,682 authorized for AI-voice calls, 15,318 quarantined — 6,821
  because their consent names a different company, 4,621 because the number is
  Canadian/Caribbean (+1 but outside the US), 2,706 with no consent, 2,658 with
  undocumented consent, 801 flagged DNC by the source, 532 with no usable state.

## Inventory, scrub, qualification, enqueue

| Step | Volume | Time |
| --- | ---: | ---: |
| Inventory report (API) | 27,000 leads | 0.37 s |
| National DNC scrub (FTC provider) | 27,000 | 19.6 s (1,376/s) |
| Qualify (triage + score) | 10,784 | 26.9 s (400/s) |
| Campaign enqueue | 12,026 | 1.2 s |

## Sustained dialing

20 concurrent calls, 300 calls/minute provider limit, campaign budget $600.

| Measure | Result |
| --- | --- |
| Throughput | 1,950 attempts in 7 min 11 s — **271/min sustained**, held at the 300/min limit |
| Dial-queue query (8,000+ pending) | 3.1 ms (index scan) |
| Kafka consumer lag | 0 throughout |
| Budget ceiling | outbound **halted itself at $599.08 of $600**, 0 calls left in flight |
| Calendar | 50 appointments = exactly the configured capacity; ≤ 4 per day; **0 double-booked, 0 outside hours** |
| Funnel | 25.6 % answered, 45.6 % of answered quoted, 8.2 % of answered opted out (all suppressed) |
| Unit cost (simulated mix, AI at $0.15/min) | $0.31 per attempt, $1.20 per answered call |

Concurrency safety (integration tests): 25 simultaneous bookings of one slot →
exactly 1 succeeds; with a 2-per-day cap, 15 simultaneous bookings across a
day's other slots → 1 more succeeds.

## Failure and recovery

| Scenario | Result |
| --- | --- |
| Core killed (SIGKILL) with 20 calls in flight | back to healthy in 10–11 s; outbound state, queue and spend intact |
| Status callbacks lost in that crash | **17 calls settled from provider state 129 s after the crash** (reconciler); before this change every lost callback held its line for 10 minutes |
| Calls with no provider id (pre-change) | freed by the 10-minute reaper, as designed |
| Kafka stopped | API ingest fails loudly (not a silent 202), core stays up, consumer resumes when Kafka returns |
| Stuck `dialing` row | reaped |
| ai-agent restarted | recovers |
| Campaign budget raised after exhaustion | campaign → paused → re-activated → dialing resumed; halted again at **$899.32 of $900** |
| Daily ceiling | dialer **held** (not halted) at $928.56 of $929.32; no attempts afterwards; outbound stayed authorized |

## Defects the load test found (fixed)

1. **Ceilings were not hard.** After the daily hold, calls already in flight
   finished and pushed the day to $929.86 against a $929.32 limit (+$0.54): the
   per-call reserve assumed calls never ran long, and follow-up texts were not
   budget-checked. Now Twilio ends every call at `MAX_CALL_MINUTES` (default
   15), the reserve is the cost of a full-length call, and every text is checked
   against the daily and campaign budgets before it is sent. **Re-verified:**
   with a $60 daily limit the day closed at **$57.72** including every call that
   finished after the hold (longest call 720 s).
2. **Dialing continued after the calendar was full.** The 50 slots were gone
   after about 1,150 attempts (≈ $7–9 per appointment up to that point); the
   remaining ~1,800 attempts (~$560) could not produce an appointment. The
   dialer now holds while the agent's calendar has no open slot
   (`PAUSE_DIALING_WHEN_CALENDAR_FULL`). **Re-verified:** with a 1-per-day
   calendar, dialing stopped by itself once all 12 slots in the horizon were
   booked — 0 attempts in the 30 s after the hold, no day over its cap.
3. **Lost status callbacks stalled dialing for 10 minutes.** The provider call
   id is now stored and stuck calls are reconciled against the provider.
4. **Kafka reserved 1 GB of RAM** (image default heap). The production overlay
   caps it at 512 MB.
5. **Canadian numbers** (valid +1 numbers, not covered by the US registry) were
   not flagged; they are now quarantined (`non_us_number`).

## Memory and CPU

Measured during the dialing run (20 concurrent simulated calls):

| Service | Memory | CPU |
| --- | ---: | ---: |
| eastwood-core | 90–94 MB | 15–35 % of one core at ~270 calls/min |
| ai-agent | 35–52 MB | low |
| PostgreSQL | 240–385 MB | < 5 % |
| Kafka | 1.0 GB at the image default; 512 MB heap cap in production | low |
| Grafana / Prometheus | 117 MB / 63 MB | low |
| Redis, nginx | ~9 MB each | — |

Database: ~2.5 KB per lead including consent evidence and indexes — a
27,000-lead book is about 90 MB.

The simulated calls carried no audio. Real calls stream 8 kHz audio both ways
through ai-agent (≈ 0.2–0.3 Mbit/s per call) and hold an OpenAI realtime socket;
at the 1–3 concurrent calls a part-time operation needs, that is negligible.

## Recommended server

| Use | Size | Why |
| --- | --- | --- |
| Part-time production (1–3 concurrent calls, ≤ 30 calls/min) | **2 vCPU / 4 GB RAM, 40 GB disk** | the stack needs ~1.2 GB with Kafka capped; 4 GB leaves room for Postgres cache, backups and upgrades |
| Headroom / 10–20 concurrent calls | 4 vCPU / 8 GB RAM (≈ $48/mo) | measured stack at 20 concurrent fits comfortably |

Size the campaign, not the server: at 271 calls/minute the system outran a
50-slot calendar in under five minutes. For one licensed agent working part
time, `max_concurrent_calls` 1–3 and a daily budget matched to open slots are
the right settings.
