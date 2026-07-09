import type { AcquisitionTask, LeadContext, SessionState } from "../types.js";

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

/** Tier-1 screening tasks still outstanding: age, tobacco, conditions, meds, height/weight. */
export function outstandingTier1(lead: LeadContext): AcquisitionTask[] {
  return (lead.acquisitionPlan ?? []).filter((task) => !isFollowupTask(task));
}

/** True once every Tier-1 screening question has been captured. */
export function discoveryComplete(lead: LeadContext): boolean {
  return outstandingTier1(lead).length === 0;
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

  // Anything still on the agenda needs a way to be recorded — Tier-1 screening,
  // or a Tier-2 follow-up that add_followup_questions just appended.
  if ((lead.acquisitionPlan?.length ?? 0) > 0) {
    allowed.add("update_lead_field");
  }

  if (discoveryComplete(lead)) {
    // Follow-ups stay available so a condition confirmed late in the call can
    // still pull its Tier-2 questions onto the agenda.
    allowed.add("add_followup_questions");
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
