/**
 * The discovery flow: which question the call is on, as an explicit machine.
 *
 * ── Why this exists ───────────────────────────────────────────────────────
 * Nothing owned "which question are we on". The turn gate ruled on whether a
 * turn produced an answer; update_lead_field ruled on whether a value could be
 * stored; but the choice of what to ask NEXT was the model's, taken from a
 * prose agenda in the system prompt. So progress depended on the model
 * remembering where it was, and a traced call spent 208 seconds without ever
 * establishing a single slot that stuck.
 *
 * `salesMachine` looks like it should do this job and does not: nothing ever
 * sends ADVANCE, so it sits in `greeting` for the entire call. Its state value
 * is not a fact about the conversation, which is why deriveAllowedTools stopped
 * reading it.
 *
 * ── How it works ──────────────────────────────────────────────────────────
 * The state is DERIVED, not stored: the current step is the first one whose
 * slot is not yet captured and which has not been abandoned. That is the same
 * pattern deriveAllowedTools uses, and for the same reason — a second copy of
 * "where are we" is a second thing that can be wrong, and the captured slots
 * are already the authority.
 *
 *   ASK_BENEFICIARY
 *      → wait → transcript
 *      → valid?  yes → slot CONFIRMED → the step is satisfied → ASK_COVERAGE
 *                no  → one clarification → wait → still bad → step abandoned
 *                                                            → ASK_COVERAGE
 *
 * The machine owns the TARGET and the ADVANCE. It does not own the wording —
 * `question` is a reference phrasing, not a script to read out. Driving the
 * exact sentence from here would trade one failure (a lost call) for another
 * (an IVR), and the persona is the reason these calls work at all.
 */

import type { QuestionExpectation } from "./turnGate.js";
import { CAPTURE, tier1HealthComplete } from "./prospectProfile.js";
import type { LeadContext, SessionState } from "../types.js";

export type DiscoveryStepId =
  | "ASK_PURPOSE"
  | "ASK_BENEFICIARY"
  | "ASK_DISPOSITION"
  | "ASK_COVERAGE"
  | "ASK_RATING_SEX"
  | "ASK_TOBACCO"
  | "ASK_HEALTH"
  | "READY_TO_QUOTE";

export interface DiscoveryStep {
  id: DiscoveryStepId;
  /** Capturing ANY of these satisfies the step — "who is protected" is
   *  answered by a name or a relationship, and either is enough to quote. */
  captureKeys: string[];
  /** What she is establishing, in plain words. This is what the model is told. */
  goal: string;
  /** Reference phrasing. Sets the target and the answer shape, not the script. */
  question: string;
  expectation: QuestionExpectation;
  /** False for steps a quote can be produced without. An abandoned required
   *  step still advances the call — it just leaves a gap the brief will show. */
  requiredForQuote: boolean;
  /**
   * For steps whose completion is not one capture key. Health screening is
   * tracked by the acquisition plan (several agenda items, resolved server
   * side), and `missingForQuote` already reads it via tier1HealthComplete —
   * so the flow defers to that rather than inventing a second definition of
   * "health is done" that can disagree with the quote gate.
   */
  satisfiedBy?: (session: SessionState, lead: LeadContext) => boolean;
}

/**
 * Coverage basics first, health second. That order is deliberate and is stated
 * in the prompt for the same reason: a call that spends its time on medical
 * history and never discusses coverage has failed, however complete the health
 * record.
 */
export const DISCOVERY_FLOW: readonly DiscoveryStep[] = [
  {
    id: "ASK_PURPOSE",
    captureKeys: [CAPTURE.coveragePurpose],
    goal: "why they are looking into coverage, and what it is for",
    question: "What's got you looking into this?",
    expectation: "free_text",
    requiredForQuote: true,
  },
  {
    id: "ASK_BENEFICIARY",
    captureKeys: [
      CAPTURE.beneficiaryName,
      CAPTURE.beneficiaryRelationship,
      CAPTURE.peopleProtected,
    ],
    goal: "who the coverage is meant to protect",
    question: "Who are you mainly trying to make sure is protected?",
    expectation: "free_text",
    requiredForQuote: true,
  },
  {
    id: "ASK_DISPOSITION",
    captureKeys: [CAPTURE.burialOrCremation],
    goal: "whether they are planning on burial or cremation",
    question: "Would this mostly be for burial or cremation costs?",
    expectation: "choice",
    // Not in missingForQuote, so a quote can be produced without it — but the
    // engine reads it to size the face amount, so an unknown here quietly
    // costs option quality rather than blocking anything.
    requiredForQuote: false,
  },
  {
    id: "ASK_COVERAGE",
    captureKeys: [CAPTURE.desiredCoverageAmount, CAPTURE.monthlyBudget],
    goal: "how much coverage they want, or what they can afford monthly",
    question: "Did you have a coverage amount in mind, or a monthly budget?",
    expectation: "amount",
    requiredForQuote: true,
  },
  {
    id: "ASK_RATING_SEX",
    captureKeys: [CAPTURE.ratingSex],
    goal: "rating sex, which the quote engine requires and you may never infer",
    question: "For rating purposes, are you male or female?",
    expectation: "choice",
    requiredForQuote: true,
  },
  {
    id: "ASK_TOBACCO",
    captureKeys: [CAPTURE.tobacco],
    goal: "tobacco or nicotine use in the past twelve months",
    question: "Have you used any tobacco or nicotine in the past twelve months?",
    expectation: "yes_no",
    requiredForQuote: true,
  },
  {
    id: "ASK_HEALTH",
    captureKeys: [CAPTURE.conditions],
    goal: "the combined health screening — a list, where none is a full answer",
    question:
      "Have you been diagnosed with or treated for any of the following in the last 5 years: cancer, heart disease, diabetes, stroke, COPD, or kidney disease?",
    expectation: "yes_no",
    requiredForQuote: true,
    satisfiedBy: (_session, lead) => tier1HealthComplete(lead),
  },
] as const;

function satisfied(step: DiscoveryStep, session: SessionState, lead: LeadContext): boolean {
  if (step.satisfiedBy) return step.satisfiedBy(session, lead);
  // `captured` holds CONFIRMED values only — a HEARD candidate is deliberately
  // not here, so a slot still being read back does not advance the flow.
  return step.captureKeys.some((k) => {
    const v = session.captured[k];
    return v !== undefined && v !== null && String(v).trim() !== "";
  });
}

/**
 * The step the call is on: the first that is neither captured nor abandoned.
 * Null once discovery is done, which is the READY_TO_QUOTE state.
 */
export function currentStep(
  session: SessionState,
  lead: LeadContext,
  skipped: ReadonlySet<DiscoveryStepId>
): DiscoveryStep | null {
  for (const step of DISCOVERY_FLOW) {
    if (skipped.has(step.id)) continue;
    if (!satisfied(step, session, lead)) return step;
  }
  return null;
}

export function currentStepId(
  session: SessionState,
  lead: LeadContext,
  skipped: ReadonlySet<DiscoveryStepId>
): DiscoveryStepId {
  return currentStep(session, lead, skipped)?.id ?? "READY_TO_QUOTE";
}

/** Steps that were abandoned or never answered — what the brief must flag. */
export function unresolvedSteps(session: SessionState, lead: LeadContext): DiscoveryStep[] {
  return DISCOVERY_FLOW.filter((s) => !satisfied(s, session, lead));
}

/**
 * The block appended to the session instructions each turn, naming where the
 * call actually is. Without it the model is choosing the next question from
 * prose and its own memory of what it already asked, which is exactly what
 * left a call circling one slot for three minutes.
 *
 * Deliberately short. It states the target and the boundary; it does not
 * restate the persona, the compliance rails, or the phrasing.
 */
export function discoveryStateBlock(
  session: SessionState,
  lead: LeadContext,
  skipped: ReadonlySet<DiscoveryStepId>
): string {
  const step = currentStep(session, lead, skipped);
  const done = DISCOVERY_FLOW.filter((s) => satisfied(s, session, lead)).map((s) => s.id);
  const lines: string[] = [
    "",
    "── WHERE THIS CALL ACTUALLY IS ──────────────────────────────────────",
    "This is computed from what you have actually captured, not from your",
    "memory of the conversation. It is authoritative. Trust it over your own",
    "sense of what you have already covered.",
    "",
    `  CURRENT STEP: ${step?.id ?? "READY_TO_QUOTE"}`,
  ];

  if (step) {
    lines.push(
      `  You are establishing: ${step.goal}.`,
      `  Reference phrasing: "${step.question}"`,
      "  Ask it in your own words, then STOP and listen. One question, this one.",
      "  Do not ask about anything further down the list until this is recorded",
      "  or you have been told to move past it."
    );
  } else {
    lines.push(
      "  Discovery is complete. Do not ask further discovery questions.",
      "  Call get_quote now and present the real options."
    );
  }

  if (done.length > 0) {
    lines.push("", `  ALREADY RECORDED — never ask these again: ${done.join(", ")}`);
  }
  if (skipped.size > 0) {
    lines.push(
      `  ABANDONED — you could not get these and they are CLOSED: ${[...skipped].join(", ")}`,
      "  Do not return to them. The licensed agent will confirm them later."
    );
  }
  return lines.join("\n");
}
