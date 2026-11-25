# Carrier quoting: sources, access, status and costs

How Eastwood gets real carrier prices, carrier by carrier. Rule: a premium
reaches a prospect only from a **verified** rate sheet. That is the carrier's
own rate book, imported as data and checked to the cent against the carrier's
own quote tool, covering every rate class. Nothing is estimated or interpolated.

Researched 2026-11-25. Vendor prices change; confirm before paying.

## Bottom line

- **Cheapest authorized source: your carriers' own rate books, through your appointments.**
  - Final-expense carriers give current rate books to appointed agents, through their agent portals or your IMO/FMO's contracting portal. The same portals have the carrier's agent quote tool, which is what verification compares against.
  - Neither needs a subscription. The cost is the appointment itself (state appointment fees vary).
- **Can existing rate sheets be imported directly without a subscription? Yes**, if the file is a digital rate book (PDF with a text layer, XLSX or CSV).
  - `python -m scripts.rates extract` reads the common layouts: per-$1,000 tables, premium grids by face amount, two-line and merged-cell headers, books without a tobacco split, and tables continued across pages.
  - Scanned books are refused: an OCR misread in a cell no spot check covers would misquote someone.
- **Tested on a real carrier chart.** On Mutual of Omaha's public Guaranteed Whole Life chart (effective August 1, 2026), the extractor read all 410 printed premiums exactly ([chart](https://cdn.mutualofomaha.com/mutualofomaha/documents/pdfs/life-insurance/digital-fulfillment/Rate-Chart-National.pdf)).
  - It was **not** loaded as a quoting source. It is the direct-to-consumer chart, and agency pricing differs. A broker reports $41.01 through an agency versus $50.00 direct for a 65-year-old woman, $10,000 ([Choice Mutual](https://choicemutual.com/mutual-of-omaha-living-promise)).
  - Always load the rate book for the channel you sell through.
- **What is loaded today: no carrier rates.** No rate book exists in the repository or the workspace. The only sheet ever loaded is the synthetic test sheet, which production refuses.
- **When to pay:**
  - **Term life:** a comparative engine (Compulife) is the economical route, since its rates depend on underwriting classes and face bands.
  - **Medicare Supplement:** priced by rating area, which Eastwood's rate sheets don't model.
  - **Final expense:** paying is not needed.

## The five options, in your priority order

| # | Option | Finding | Outcome |
| --- | --- | --- | --- |
| 1 | Integrations already in the repo | **CMS adapter:** a placeholder dataset id, and it pulls the first 50 plans in a *state*, though Medicare Advantage plans are priced by county. **Formula adapter:** a fitted curve, not carrier data. **Rate-table adapter:** no loader; ignored sex and dates. **Rate-sheet engine:** works. | Completed the rate-sheet path. The other three stay internal; they never price a prospect. |
| 2 | Carrier rate sheets | Agent-only for these carriers. Public charts exist only for some direct-to-consumer products, at different prices. Forum reports say current versions are found through the carrier or IMO portal ([insurance-forums](https://insurance-forums.com/community/threads/final-expense-rate-sheets-and-apps.50221), [agenttech](https://agenttech.io/blog/top-final-expense-insurance-carriers-2026)). | Built the importer for them. |
| 3 | Your agency's resources | Carrier appointments and portal logins, through your IMO/FMO. FMO-provided quoting screens such as Insurance Toolkits are good for spot checks; they are not a data feed unless the API is licensed separately ([Insurance Toolkits](https://landing.insurancetoolkits.com/)). | The runbook's rate step uses exactly these. |
| 4 | Official / comparative APIs | No public quoting API from these final-expense carriers. CMS publishes Medicare Advantage landscape files for free, at county level ([KFF](https://www.kff.org/medicare/medicare-advantage-2026-spotlight-a-first-look-at-plan-premiums-and-benefits/)). CSG Actuarial's API has custom pricing, documentation on request, and covers Medicare Supplement ([CSG](https://www.csgactuarial.com/news/p5bbsrrmrn8u0n140ookei6zz0xgc2)). | Documented, not built: nothing to integrate without access. |
| 5 | Paid integrations | Compulife API plan; final-expense quoting engines with unpublished API prices (see costs below). | Not needed for final expense. Optional for term life. |

## Carrier-by-carrier

These are the carriers already profiled in `domain/placement/carrier_profiles_v0_1.json`.

| Carrier, product | Authoritative price source | Access you need | Eastwood status | Cost | What remains |
| --- | --- | --- | --- | --- | --- |
| American Amicable, final expense (Senior Choice) | Carrier rate guide, by age and face amount | Appointment; agent portal login. Third-party guides list two different login paths, so confirm with the carrier. | Importable (grid or per-$1,000) | $0 with appointment | Get the current rate book → extract → price the check cases in AmAm's quote tool → verify |
| Mutual of Omaha, Living Promise (agency channel) | Producer-portal rate chart and quote tool ([producer portal](https://producer.mutualofomaha.com/enterprise/portal/home/products/life/whole-life)) | Appointment; producer portal | Importable; extractor proven on a real Mutual of Omaha chart layout | $0 with appointment | The **agency-channel** rate book, not the public direct chart → extract → verify |
| Royal Neighbors, Ensured Legacy (simplified issue, graded, guaranteed issue; $5,000–$40,000) | Agent rate guide; rates are not published publicly ([product page](https://royalneighbors.org/products/ensured-legacy-final-expense)) | Appointment; agent portal (a third-party chart lists agent.royalneighbors.org; confirm) | Importable; tiers map to level / graded / guaranteed | $0 with appointment | Rate book plus the tier questions from the application → verify |
| Transamerica, final expense | Rate materials on its agent site, ANI ([example: Transamerica rate flyer, agent-only](https://ani.transamerica.com/ani/Uploads/Pages/54690/Final_Expense_Rate_Reductions_Flyer.pdf)) | Appointment; ANI login | Importable | $0 with appointment | Rate book → extract → verify |
| Prosperity Life, final expense | Carrier rate book (its site lists Final Expense Whole Life; an agent portal was not found publicly) ([Prosperity Life](https://www.prosperitylife.com/?p=49)) | Appointment; ask agent services | Importable | $0 with appointment | Rate book → extract → verify |
| Aetna / CVS, Medicare Supplement | Carrier rates by rating area, age, sex and tobacco | Appointment; carrier portal | **Not integrated.** Rate sheets don't model rating area; Medicare isn't quoted by text (CMS marketing rules). | $0 portal; CSG API custom | Needs a rating-area dimension, or a paid source; a Medicare-compliant script |
| Aetna / CVS, Medicare Advantage | CMS landscape files (plan premiums by county), free | None (public data) | **Not integrated.** The legacy CMS adapter isn't county-level. | $0 | A county lookup per lead and Medicare marketing compliance; out of scope for SMS-first |
| Humana, Medicare Advantage | CMS landscape files; Humana Vantage quoting ([Vantage](https://www.humana.com/agent/ad/vantage)) | Appointment; Vantage | Not integrated (as above) | $0 | As above |
| Humana, Medicare Supplement | Vantage quoting; rate changes are announced by state ([Sept 2026 notice](https://assets.humana.com/is/content/humana/Sales%20Compass-Premium%20Rate%20Changes%20September%202026pdf)) | Appointment; Vantage | Not integrated | $0 portal | As for Aetna Medicare Supplement |

**Life (term)** isn't in the carrier profiles. If you want term quotes by text, Compulife covers 100+ life carriers, and its API plan is the economical feed. Eastwood would need a Compulife adapter, built against its API documentation once subscribed. Until then life leads get no number and are booked with the agent; the acceptance test confirmed this.

## Exact costs

| Item | Price | Source / note |
| --- | --- | --- |
| Carrier rate books and agent quote tools | **$0** with appointment | State appointment fees vary by state and carrier |
| CMS Medicare Advantage landscape files | **$0** | Public |
| Compulife, API plan | **$429–480 / yr** listed; **$900 / yr** as an add-on to the Premium plan; up to ~$1,500 / yr by volume | Listing sites disagree: [Capterra](https://www.capterra.com/p/81783/Compulife-Quote-Software/pricing/), [FitGap](https://us.fitgap.com/products/032079/compulife-quote). Confirm with Compulife. |
| Insurance Toolkits (final-expense quoter, screen only) | **$39.99 / mo**; API sold separately, price not published | [Insurance Toolkits](https://landing.insurancetoolkits.com/) |
| NinjaQuoter (screen) | **$19 / $39 / $69 per month**; API exists, price not published | [Software Finder](https://softwarefinder.com/insurance-software/ninjaquoter) |
| FEXQuotes API | Not published; a forum estimate puts these engines at ~$900 / yr for unlimited API calls | [insurance-forums](https://www.insurance-forums.com/community/threads/fexquote-vs-ninja-quoter-for-final-expense-api.108850) (anecdotal) |
| CSG Actuarial API (Medicare Supplement) | Custom quote | [CSG Actuarial](https://www.csgactuarial.com/news/p5bbsrrmrn8u0n140ookei6zz0xgc2) |

## How a rate book becomes quotes

1. Download the current rate book for one plan and one channel from the carrier's agent portal (or your IMO's).
2. **Extract:** `python -m scripts.rates extract book.pdf --unit <unit> --out ./plan`. Choose the unit by what the book's numbers are:

   | `--unit` | The book's numbers are |
   | --- | --- |
   | `per_1000_annual` | annual rate per $1,000, plus an annual policy fee and modal factors |
   | `per_1000_monthly` | monthly rate per $1,000, plus a monthly fee |
   | `flat_monthly` | monthly premium per face amount |
   | `flat_annual` | annual premium per face amount |

   Pass `--tier` / `--sex` / `--tobacco` (`any` for a book with no tobacco split) when titles don't say. Read `report.txt`: it lists every table it read, the classes, the age span, gaps, and any rate that falls with age.
3. **Fill in the meta.** Fill every `null` in `meta.template.json` from the rate book's own instructions:
   - policy fee, modal factors and rounding
   - face limits, states and effective dates
   - the questions that place a prospect in graded or guaranteed tiers

   The report quotes the document lines that mention them. Save the result as `meta.json`.
4. **Spot checks:** `python -m scripts.rates checks-template ./plan/rates.csv --unit <unit> --out ./plan/checks.csv`. Price each case in the **carrier's own quote tool** and fill in `expected`.
5. **Import and verify:** `python -m scripts.rates import ./plan/rates.csv --meta ./plan/meta.json`, then `verify <id> --checks ./plan/checks.csv --verified-by "<you>"`. Every check must match to the cent, and every rate class needs at least one check. Anything else leaves the sheet a draft that quotes no one.
6. **Maintain:**
   - Run `python -m scripts.rates list` to see what is live.
   - When the carrier changes rates, load the new book and `retire` the old sheet.
   - An expired sheet stops quoting by itself.
