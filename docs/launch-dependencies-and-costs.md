# Launch dependencies, credentials, regulatory prerequisites, and costs

What Eastwood still needs from outside the codebase before a lawful, operational
launch, and what it costs. The application work is done (see the verification
record in [`go-live-checklist.md`](go-live-checklist.md)); everything here is an
account, a credential, a registration, a legal sign-off, or money.

Prices were checked on 2026-11-24 and change often. Confirm each one in the
provider's console or price sheet before paying. This is not legal advice; the
regulatory section lists questions to settle with counsel.

---

## Minimum cost to launch

**Scope assumed for the minimum:** one supervised test campaign, final-expense
leads only, one state, numbers drawn from **five or fewer phone area codes**,
5–10 calls, the stack on hardware you already own behind a free named tunnel,
and a domain you already control.

| | Amount | What it is |
| --- | --- | --- |
| One-time | **≈ $19.50** | A2P 10DLC low-volume brand ($4.50) + campaign vetting ($15) |
| Monthly | **≈ $2.65** | Twilio local number ($1.15) + low-volume 10DLC campaign ($1.50) |
| Usage, whole test | **≈ $5–20** | ~50–100 connected minutes at ≈ $0.07–0.17/min, plus AMD and a few SMS |
| FTC registry | **$0** | The first five area codes are free |
| **First month** | **≈ $27–42** | Add ≈ $10–15/yr if you need to register a domain |

**Single-state production campaign on a VPS:**

| | Amount |
| --- | --- |
| One-time | ≈ $61 (standard 10DLC brand with secondary vetting ≈ $46 + campaign vetting $15) |
| Yearly | FTC registry: **$85 × (area codes − 5)** — e.g. 20 area codes = $1,275; 30 = $2,125 |
| Monthly | ≈ $59 (4 vCPU / 8 GB VPS ≈ $48 + number $1.15 + campaign $10) |
| Usage | ≈ $0.07–0.17 per connected minute; $0.0075 per answered call (AMD); ≈ $0.012–0.014 per SMS segment |

The registry is billed **per phone area code, not per state** — a lead's mobile
number frequently carries an out-of-state area code. Count the distinct area
codes in the list you intend to dial. Numbers in unsubscribed area codes stay
`unknown` and are never dialed, so the subscription also sets the campaign's
reach.

---

## Mandatory expenses

| Item | Cost | Notes |
| --- | --- | --- |
| FTC National Do Not Call Registry subscription (SAN) | $0 for ≤5 area codes; then **$85 per area code per year** (FY2027, from Oct 1 2026); $43 per extra area code for a half year; $23,425 nationwide cap | Required to telemarket. Renew annually. Eastwood refuses production boot without it and needs the dataset refreshed at least every 31 days. |
| Twilio local number | **$1.15/mo** | Caller ID for outbound calls and SMS sender. |
| A2P 10DLC registration | Brand **$4.50** (low-volume) or **≈ $46** (standard, incl. secondary vetting), one-time; campaign vetting **$15** one-time (re-charged on resubmission); campaign **$1.50–10/mo** | Carriers block unregistered application-to-person SMS. Eastwood texts follow-ups, consent requests and agent briefs. Approval can take days to weeks — start early. |
| Twilio usage | Outbound voice **$0.014/min**; AMD **$0.0075** per answered call; SMS **≈ $0.0083/segment + $0.0035–0.005** carrier fee; $0.001 per failed message | |
| OpenAI usage | Realtime audio ≈ **$32 / $64 per 1M** audio input / output tokens (third-party reported) → **≈ $0.05–0.15 per call minute**; `gpt-4o-transcribe` ≈ **$0.006/min** | The repo pins `gpt-realtime-2`; confirm that model's price and availability (one source reports the original `gpt-realtime` retiring on 2027-01-20). |
| Stable HTTPS endpoint | **$0** (existing machine + free named tunnel + a domain you own) **or ≈ $42–48/mo** (4 vCPU / 8 GB VPS) | Twilio must reach `PUBLIC_BASE_URL` for every call. A laptop that sleeps drops callbacks — fine for a supervised test, not for a campaign. TLS is free (Let's Encrypt or the tunnel). Domain ≈ $10–15/yr if you have none. |
| Licensed closing agent | $0 if already licensed; Michigan resident producer: $10 application + $5 transaction fee, ≈ $41 per exam attempt, plus pre-licensing education | Must be licensed (life / health lines as applicable) in every campaign state, with carrier appointments. |
| Credential rotation | $0 | Twilio auth token and OpenAI key (see the go-live checklist §1). |

## Optional expenses

| Item | Cost | Without it |
| --- | --- | --- |
| Calendly Standard | ≈ $10–12 per seat per month | Booking still persists the requested time and alerts the agent; the prospect is never told a calendar booking is confirmed. Confirm Standard includes the Scheduling API you need. |
| Extra registry area codes | $85 each per year | Mandatory for any area code you want to dial; optional only in that you can keep the campaign to five. |
| Legal review (TCPA / insurance) | Get a fixed-fee quote | Strongly recommended before the first live call — see below. |
| Branded caller ID / CNAM | Not priced here | Lower answer rates. |
| Lead purchases | Vendor-dependent | — |
| Monitoring | $0 | Prometheus and Grafana are self-hosted in the stack. |

---

## External dependencies and required credentials

| Dependency | Used for | Credentials / configuration | State on 2026-11-24 |
| --- | --- | --- | --- |
| Twilio Programmable Voice, Media Streams, AMD | Outbound calls, audio to Imogen | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER`; callbacks under `PUBLIC_BASE_URL` | Configured values are the pre-scrub exposed ones — **rotate** |
| Twilio Messaging | Follow-ups, consent requests, agent briefs, opt-out intake | 10DLC brand + campaign; inbound webhook `POST {PUBLIC_BASE_URL}/twilio/sms` | Not configured |
| OpenAI Realtime + `gpt-4o-transcribe` | Imogen's voice and transcript grounding | `OPENAI_API_KEY` | Exposed key — **rotate** |
| FTC National DNC Registry | Clearing numbers to dial | `DNC_SAN`, `DNC_REGISTRY_USERNAME`, `DNC_REGISTRY_PASSWORD`, `DNC_AREA_CODES`, `DNC_REGISTRY_DOWNLOAD_URL` | Not configured |
| Public HTTPS origin | Every Twilio webhook | `PUBLIC_BASE_URL` (stable, HTTPS, not a temporary tunnel) | Temporary ngrok URL in dev only |
| Licensed agent's phone | Handoff briefs, outcome SMS, live-transfer alerts | `AGENT_NOTIFICATION_PHONE_E164`, `RYAN_PHONE_NUMBER` | Required by the production overlay |
| Internal secrets | Service-to-service auth, databases, Grafana | `EASTWOOD_API_KEY`, `POSTGRES_PASSWORD`, `GRAFANA_ADMIN_PASSWORD` | Generated in the local `.env.production` |
| Calendly *(optional)* | Real calendar bookings | `CALENDLY_ENABLED`, `CALENDLY_ACCESS_TOKEN` | Disabled |
| Facebook Lead Ads *(optional)* | Inbound leads | `FACEBOOK_APP_SECRET`, `FACEBOOK_VERIFY_TOKEN` | Not configured |
| CMS Data API, NPPES NPI | Medicare quotes, provider lookup | None (public) | — |
| OpenFEC *(optional)* | Donor enrichment | `OPENFEC_API_KEY` (not yet passed through `docker-compose.yml`) | Not configured |

---

## Regulatory prerequisites

Settle each with counsel before the first live call.

1. **National DNC (FTC Telemarketing Sales Rule).** Active SAN for every area
   code dialed; data no older than 31 days. *Eastwood enforces this: `unknown`
   and `blocked` never dial, and production refuses to boot without the FTC
   provider.*
2. **AI voice is an "artificial voice" under the TCPA** (FCC Declaratory Ruling
   FCC 24-17, Feb 2024). Telemarketing calls with Imogen need **prior express
   written consent** for cell and residential lines. *Eastwood auto-dials only
   leads with recorded express written consent.* Confirm your lead vendors'
   consent language names your agency and covers AI / artificial-voice calls,
   and keep the evidence (Eastwood stores it in `consents.evidence_pack`). The
   FCC's 2024 proposal on AI-specific consent and in-call disclosure was not
   confirmed as final when this was written — check its status.
3. **Artificial-voice call content** (47 CFR §64.1200(b)) sets identification
   requirements and, for telemarketing, an automated opt-out mechanism. Imogen
   discloses she is an AI and ends the call on any spoken do-not-call request,
   but her opening does **not** announce an opt-out mechanism. Have counsel
   approve the opening script.
4. **Revocation of consent.** Honor revocation "by any reasonable means" within
   10 business days; the FCC treats replies such as *stop, quit, revoke, opt out,
   cancel, unsubscribe, end* as conclusive. *Eastwood suppresses immediately on
   a spoken request or a texted STOP — once the Twilio inbound webhook is set.*
   The FCC amended these rules again in late 2026 ("revoke all" delayed to
   2027-01-31); monitor.
5. **Calling hours.** Eastwood enforces the federal 8 a.m.–9 p.m. local window.
   Some states set narrower windows or day restrictions — check each state
   before adding it to `ACTIVE_STATES`.
6. **State telemarketing law.** Michigan adopted the federal registry as its
   state list, and its telephone-solicitation statute (MCL 445.111) appears to
   carve out solicitation by licensed insurance agents — confirm. Before adding
   any other state, check its registration or bond requirements, state DNC list,
   and any "mini-TCPA" statute.
7. **Call recording and transcription.** Imogen transcribes every call.
   All-party-consent states require consent; add a disclosure at the start of
   the call before dialing them.
8. **Insurance licensing.** The closing agent must be licensed in each lead's
   state and appointed with the carriers. Have counsel confirm that Imogen's
   discovery questions and labeled estimates do not amount to solicitation
   requiring a license in the campaign states.
9. **Medicare products.** Medicare Advantage / Part D sales calls carry CMS
   marketing rules: the TPMO disclaimer before plan benefits are discussed,
   call-recording retention (CMS's CY2027 rule shortens it from 10 to 6 years),
   and scope-of-appointment rules. Launch with **final-expense leads only**;
   Eastwood has no product-line switch yet, so filter the import.
10. **SMS.** A2P 10DLC registration; every template already carries "Reply STOP".

---

## Sources

- FTC FY2027 registry fees: [FTC press release (Aug 2026)](https://www.ftc.gov/news-events/news/press-releases/2026/08/ftc-announces-2027-telemarketer-fees-access-national-do-not-call-registry), [Greenspoon Marder](https://www.gmlaw.com/news/compliance-reminder-ftc-increases-national-do-not-call-registry-fees-for-fy-2027/)
- Twilio: [voice pricing (US)](https://www.twilio.com/en-us/voice/pricing/us), [SMS pricing (US)](https://www.twilio.com/en-us/sms/pricing/us), [A2P 10DLC](https://www.twilio.com/en-us/phone-numbers/a2p-10dlc), [T-Mobile carrier fee change](https://support.twilio.com/hc/en-us/articles/44609260499995-T-Mobile-Messaging-Carrier-Fee-Changes-January-2026), [10DLC fee summary](https://a2pready.com/a2p-10dlc-fees/)
- OpenAI: [Realtime pricing summary (layer3labs)](https://www.layer3labs.io/guides/openai-realtime-api-pricing), [cost per minute (cekura)](https://www.cekura.ai/blogs/openai-realtime-api-pricing), [gpt-4o-transcribe model page](https://developers.openai.com/api/docs/models/gpt-4o-transcribe)
- Hosting: [DigitalOcean droplet pricing](https://www.digitalocean.com/pricing/droplets)
- Calendly: [pricing](https://calendly.com/pricing)
- Michigan licensing: [DIFS — becoming a resident producer](https://www.michigan.gov/difs/industry/licensing-ins/agnt-ins/general-info/how-to-become-licensed-as-a-resident-producer), [exam cost (Aceable)](https://insurance.aceable.com/resources/pre-license/how-much-is-the-michigan-insurance-licensing-exam/)
- AI voice under the TCPA: [WSGR on FCC 24-17](https://www.wsgr.com/en/insights/fcc-rules-ai-generated-voices-are-artificial-under-the-tcpa.html), [Federal Register NPRM (Sept 2024)](https://www.govinfo.gov/content/pkg/FR-2024-09-10/html/2024-19028.htm)
- Consent revocation: [Federal Register (Mar 2024)](https://www.govinfo.gov/content/pkg/FR-2024-03-05/html/2024-04587.htm), [Kelley Drye on the effective date](https://www.kelleydrye.com/viewpoints/blogs/ad-law-access/fcc-sets-effective-date-for-tcpa-reasonable-consent-revocation-rules.md), [DLA Piper (Oct 2026)](https://www.dlapiper.com/en/insights/publications/2026/10/fcc-tcpa-rule)
- Michigan DNC: [MPSC Do Not Call](https://www.michigan.gov/mpsc/consumer/telecommunications/do-not-call), [MCL 445.111](https://legislature.mi.gov/documents/mcl/pdf/mcl-445-111.pdf)
- Medicare marketing: [CMS 2026 marketing guidelines](https://www.cms.gov/files/document/medicare-oep-marketing-guidelines-2026.pdf), [Ritter on the CY2027 final rule](https://ritterim.com/blog/cms-improves-plan-transparency-and-choice-with-2027-ma-and-part-d-final-rule)
