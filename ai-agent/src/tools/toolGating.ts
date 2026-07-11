import type { AcquisitionTask, LeadContext, SessionState } from "../types.js";
import { quoteUnlocked, tier1HealthComplete } from "../discovery/prospectProfile.js";

/**
 * Derives which tools the model may call, from what we actually know about the
 * lead — not from the xstate machine's state value.
 *
 * The machine remains the record of conversational stage (it still receives
 * QUOTE_READY, DNC, TRANSFER, WRAP_UP, END), but nothing in the codebase ever
 * sends the ADVANCE event that moves it out of `greeting`. Gating tools on
 * `ALLOWED_TOOLS[state]` therefore left the agent permanently holding only
 * `capture_dnc` and `update_lead_field`: it could never quote, book, transfer,
 * or even hang up. Tool availability is a function of collected data, so we
 * compute it from the data.
 */

/**
 * Tier-2 follow-up tasks come back from POST /api/agenda/followups with a
 * `followup_` field prefix (see domain/acquisition.py::get_followup_tasks).
 * Everything else on the agenda is Tier-1 screening.
 */
const FOLLOWUP_FIELD_PREFIX = "followup_";

/** Available on every turn: opting out, objecting, and hanging up are never gated. */
export const ALWAYS_ALLOWED = [
  "capture_dnc",
  "end_call",
  "capture_objection",
] as const;

export function isFollowupTask(task: AcquisitionTask): boolean {
  return task.field.startsWith(FOLLOWUP_FIELD_PREFIX);
}

export function deriveAllowedTools(
  lead: LeadContext,
  session: SessionState
): string[] {
  // A prospect who has asked not to be called is no longer being sold to; the
  // only thing left is to hang up. This must precede every other rule.
  //
  // The machine models this as the `terminating` state, but nothing reads the
  // machine's state, so revocation has to live here. Deriving tools from lead
  // data alone previously left get_quote and book_appointment on the table
  // after a DNC — the prompt was the only thing stopping her from selling on.
  if (session.dncCaptured) {
    return ["end_call"];
  }

  const allowed = new Set<string>(ALWAYS_ALLOWED);

  // Recording captured answers stays available through the whole
  // discovery/selling phase: before a quote there is always more to learn
  // (needs discovery, health), and after a quote optional agenda items may
  // remain. Only once a quote exists and the agenda is empty does it drop off.
  if (session.quote === null || (lead.acquisitionPlan?.length ?? 0) > 0) {
    allowed.add("update_lead_field");
  }

  // Follow-ups become available once health screening is done, so a condition
  // confirmed late in the call can still pull its Tier-2 questions onto the agenda.
  if (tier1HealthComplete(lead)) {
    allowed.add("add_followup_questions");
  }

  // A quote requires the minimum needs-discovery profile (purpose, beneficiary,
  // and an amount or budget) on top of completed health screening — so Imogen
  // cannot price before she knows what she is pricing, or for whom.
  if (quoteUnlocked(lead, session)) {
    allowed.add("get_quote");
  }

  if (session.quote !== null) {
    allowed.add("book_appointment");
    allowed.add("transfer_live");
    allowed.add("transfer_to_human");
    allowed.add("capture_consent");
  }

  return [...allowed];
}
