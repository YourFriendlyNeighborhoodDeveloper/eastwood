# Controlled live SMS test (prepared, not run)

One real conversation, through Twilio, with **one phone you own and have
authorized**. It proves what the simulated acceptance test cannot: carrier
delivery, the real inbound webhook, and the conversation as a person actually
experiences it. The script is `eastwood-core/scripts/live_sms_test.py`. It has
not been run. Run it only after every prerequisite below is met.

## Prerequisites (all required)

| # | Item | Why |
| --- | --- | --- |
| 1 | Twilio credentials **rotated** (the old ones were exposed) and set only in the test deployment's env file | The script sends real texts |
| 2 | **A2P 10DLC brand and campaign approved**, the sending number attached to that campaign | US carriers block or filter unregistered 10DLC traffic |
| 3 | A stable **https** `PUBLIC_BASE_URL`, with the number's "A message comes in" webhook set to `PUBLIC_BASE_URL/twilio/sms` and signature validation on | Replies must reach Eastwood, signed |
| 4 | A **dedicated deployment or database** with `DEFAULT_TENANT_ID=live-test-1` (any name starting `live-test`) holding no leads | The script refuses a tenant with any other lead, so no real prospect can be texted |
| 5 | `AGENCY_DISPLAY_NAME` set to your agency exactly as the consent names it | First-text disclosure |
| 6 | `AGENT_NOTIFICATION_PHONE_E164` set (your phone, or the agent's) | Handoff and booking alerts |
| 7 | `AGENT_AVAILABILITY` set to real hours | Imogen offers real times |
| 8 | Optional: a **verified** carrier rate sheet (`scripts/rates.py import` + `verify`) | Without one, Imogen will truthfully say she can't give an exact rate and offer a call. That is still a valid test of the no-quote path |
| 9 | Daytime in the recipient's time zone (8am–9pm) | The calling-window gate applies to the test too |

The recipient number must clear the National DNC scrub (production) or be
listed in `DNC_TEST_ALLOWLIST` on a non-production test deployment. The
allowlist is refused in production by design.

## Run

```bash
# inside the test deployment's eastwood-core container
export LIVE_TEST_RECIPIENT=+1XXXXXXXXXX          # named twice on purpose
python -m scripts.live_sms_test --to +1XXXXXXXXXX --first-name <you> --last-name <you> \
    --state MI --dob 1958-04-02 --operator "<your name>" --dry-run   # guards only
python -m scripts.live_sms_test --to +1XXXXXXXXXX --first-name <you> --last-name <you> \
    --state MI --dob 1958-04-02 --operator "<your name>"             # asks for the last 4 digits
```

What happens:

1. **Guards.** The script checks every guard and prints the plan. With any refusal, nothing is created or sent.
2. **Import.** The recipient is imported as a one-row lead file through the real pipeline (consent naming your agency, DNC scrub, qualification).
3. **Campaign.** A campaign is created with a $5 budget, 25 texts a day and 5 texts a minute. Exactly one conversation is enqueued.
4. **Authorize.** Outbound is authorized and the opener goes out.
5. **Converse.** You reply from your phone as a prospect would. The script prints both sides as they happen.
6. **Halt.** When the conversation ends (booked, declined, opted out) or after `--minutes` (default 30), **outbound is halted and the campaign completed**, even on error or Ctrl-C.

## What to check

- The opener arrives, names the agency, says "AI assistant", says how to opt out.
- Your replies are understood. Every question Imogen asks is one the quote needs.
- Any premium in a text appears in **/ops → Conversations → (your conversation) → Quotes** with the same quote reference and figures. With no verified sheet, no figure appears at all.
- The booking lands on the agent's calendar, the confirmation arrives, and **/ops → Appointments** shows the full profile.
- Delivery status for each text is `delivered` (Twilio status callbacks, `/twilio/sms-status/{id}`).
- Then text STOP. The confirmation is the last text you receive, and the number is suppressed for calls and texts.

Record the result in [go-live-checklist.md](go-live-checklist.md) §7 before any real campaign.
