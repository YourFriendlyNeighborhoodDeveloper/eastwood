import type { FaceAmountReason, LeadContext, ProspectProfile, SessionState } from "../types.js";
import { UNKNOWN } from "../types.js";

/**
 * Needs-discovery gating, deterministic face-amount selection, and the handoff
 * profile — all built from captured facts, never invented by the model.
 */

// Capture keys the agent writes via update_lead_field. Kept in one place so the
// agenda (eastwood-core), the quote gate, and the profile all agree.
export const CAPTURE = {
  coveragePurpose: "coverage_purpose",
  burialOrCremation: "burial_or_cremation",
  beneficiaryName: "beneficiary_name",
  beneficiaryRelationship: "beneficiary_relationship",
  peopleProtected: "people_protected",
  desiredCoverageAmount: "desired_coverage_amount",
  monthlyBudget: "affordable_monthly_budget",
  existingCoverage: "existing_coverage",
  existingCoverageAmount: "existing_coverage_amount",
  motivation: "reason_for_coverage",
  conditions: "conditions",
  medications: "current_meds",
  tobacco: "tobacco",
  // Rating and contact facts. rating_sex is ASKED ("for rating purposes, are
  // you male or female?") and stored verbatim — never inferred from name,
  // voice, or pronouns. "unknown" when the prospect declines.
  ratingSex: "rating_sex",
  employmentStatus: "employment_status",
  heightWeight: "height_weight",
  prospectEmail: "prospect_email",
} as const;

/** Tier-1 health agenda fields. When none remain on the plan, screening is done. */
const TIER1_HEALTH_FIELDS = new Set([
  "age",
  "tobacco",
  "health_screening",
  "medications",
  "height_weight",
]);

export function tier1HealthComplete(lead: LeadContext): boolean {
  return !(lead.acquisitionPlan ?? []).some((t) => TIER1_HEALTH_FIELDS.has(t.field));
}

function has(captured: Record<string, unknown>, key: string): boolean {
  const v = captured[key];
  return v !== undefined && v !== null && String(v).trim() !== "";
}

/** Coerce "$10,000" / "10000" / 10000 to a number; NaN-safe → null. */
export function toAmount(v: unknown): number | null {
  if (typeof v === "number" && Number.isFinite(v)) return v;
  if (typeof v === "string") {
    const digits = v.replace(/[^0-9.]/g, "");
    if (digits === "") return null;
    const n = Number(digits);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/**
 * The minimum discovery a quote requires: a purpose, a beneficiary (name or
 * relationship), and either a target coverage amount or a monthly budget.
 * Existing-coverage and motivation are gathered but not gate-blocking.
 */
export function minimumDiscoveryCaptured(session: SessionState): boolean {
  const c = session.captured;
  const purpose = has(c, CAPTURE.coveragePurpose);
  const beneficiary = has(c, CAPTURE.beneficiaryName) || has(c, CAPTURE.beneficiaryRelationship);
  const target = has(c, CAPTURE.desiredCoverageAmount) || has(c, CAPTURE.monthlyBudget);
  return purpose && beneficiary && target;
}

/**
 * Rating sex must have been ASKED before quoting. A declined answer is captured
 * as "unknown" and still satisfies the gate — the quote proceeds and the
 * licensed agent confirms exact pricing. What can never satisfy it is a guess:
 * the value only ever comes from update_lead_field, never from the model's
 * judgment of a name or a voice.
 */
export function ratingSexCaptured(session: SessionState): boolean {
  return has(session.captured, CAPTURE.ratingSex);
}

/** get_quote unlocks only after Tier-1 health, minimum discovery, AND rating sex was asked. */
export function quoteUnlocked(lead: LeadContext, session: SessionState): boolean {
  return tier1HealthComplete(lead) && minimumDiscoveryCaptured(session) && ratingSexCaptured(session);
}

/**
 * What still stands between this call and get_quote, in the model's own terms.
 * Fed back on every update_lead_field so Imogen always knows the tool is
 * locked because of THESE gaps — not because quoting is unavailable.
 */
export function missingForQuote(lead: LeadContext, session: SessionState): string[] {
  const c = session.captured;
  const missing: string[] = [];
  if (!has(c, CAPTURE.coveragePurpose)) {
    missing.push("coverage purpose (capture coverage_purpose)");
  }
  if (!has(c, CAPTURE.beneficiaryName) && !has(c, CAPTURE.beneficiaryRelationship)) {
    missing.push("who the coverage protects (capture beneficiary_name or beneficiary_relationship)");
  }
  if (!has(c, CAPTURE.desiredCoverageAmount) && !has(c, CAPTURE.monthlyBudget)) {
    missing.push("a coverage amount or monthly budget (capture desired_coverage_amount or affordable_monthly_budget)");
  }
  if (!ratingSexCaptured(session)) {
    missing.push('rating sex (ask "for rating purposes, are you male or female?" and capture rating_sex)');
  }
  if (!tier1HealthComplete(lead)) {
    missing.push("the remaining health screening questions on the agenda");
  }
  return missing;
}

/**
 * Booking may replace quoting ONLY when quoting genuinely cannot happen:
 * the quote service failed (quoteFailureReason set) or the prospect explicitly
 * asked for an appointment without pricing (captured appointment_only_request).
 * While quoting is possible and not yet done, booking stays locked.
 */
export function bookingUnlocked(session: SessionState): boolean {
  if (session.quote) return true;
  if (session.quoteFailureReason) return true;
  return has(session.captured, "appointment_only_request");
}

// ── Face-amount selection ─────────────────────────────────────────────────────

const STANDARD_FACES = [5_000, 10_000, 15_000, 20_000, 25_000, 30_000];
const FINAL_EXPENSE_PURPOSES = new Set(["burial", "cremation"]);

/**
 * Coarse budget → face sizing. Approximate on purpose: it only picks which
 * standard face to quote; the real premium comes back from the quote service.
 * Monthly cost per $1,000 of final-expense coverage rises with age.
 */
function costPer1kPerMonth(age: number | null): number {
  const a = age ?? 65;
  if (a < 55) return 2.5;
  if (a < 65) return 4.0;
  if (a < 70) return 5.5;
  if (a < 75) return 8.0;
  if (a < 80) return 11.0;
  return 15.0;
}

function recommendFromBudget(budget: number, age: number | null): number {
  const per1k = costPer1kPerMonth(age);
  const affordable = STANDARD_FACES.filter((face) => (face / 1000) * per1k <= budget);
  // Largest standard face within budget; if none, the smallest standard face.
  return affordable.length > 0 ? affordable[affordable.length - 1]! : STANDARD_FACES[0]!;
}

export interface FaceSelection {
  faceValue: number;
  reason: FaceAmountReason;
}

/**
 * Choose the face amount to quote and record why. The model does not pick it:
 * it is derived from what the prospect stated. agent_default means we had no
 * stated basis at all — refused in production.
 */
export function selectFaceAmount(session: SessionState, age: number | null): FaceSelection {
  const c = session.captured;
  const requested = toAmount(c[CAPTURE.desiredCoverageAmount]);
  if (requested && requested > 0) {
    return { faceValue: requested, reason: "prospect_requested" };
  }
  const budget = toAmount(c[CAPTURE.monthlyBudget]);
  if (budget && budget > 0) {
    return { faceValue: recommendFromBudget(budget, age), reason: "budget_fit" };
  }
  const purpose = String(c[CAPTURE.coveragePurpose] ?? "").toLowerCase();
  if (FINAL_EXPENSE_PURPOSES.has(purpose)) {
    return { faceValue: 10_000, reason: "needs_estimate" };
  }
  return { faceValue: 10_000, reason: "agent_default" };
}

// ── Profile assembly ──────────────────────────────────────────────────────────

function str(v: unknown): string {
  if (v === undefined || v === null || String(v).trim() === "") return UNKNOWN;
  if (Array.isArray(v)) {
    const parts = v
      .map((x) => (x && typeof x === "object" ? (x as any).raw_text ?? (x as any).code : x))
      .filter(Boolean);
    return parts.length ? parts.join(", ") : UNKNOWN;
  }
  // A captured object (e.g. {height: "5'10", weight: 180}) must flatten to
  // readable text — String() would render "[object Object]" in Ryan's brief.
  if (typeof v === "object") {
    const parts = Object.entries(v as Record<string, unknown>)
      .filter(([, val]) => val !== undefined && val !== null && String(val).trim() !== "")
      .map(([k, val]) => `${k}: ${String(val).trim()}`);
    return parts.length ? parts.join(", ") : UNKNOWN;
  }
  return String(v).trim();
}

/** Conditions the prospect confirmed become risk flags for Ryan. Factual only. */
function riskFlags(session: SessionState): string[] {
  const conditions = session.captured[CAPTURE.conditions];
  if (!Array.isArray(conditions)) return [];
  return conditions
    .map((c) => (c && typeof c === "object" ? (c as any).raw_text ?? (c as any).code : c))
    .filter((c) => c && String(c).toLowerCase() !== "none")
    .map((c) => `condition: ${c}`);
}

/** Outstanding agenda items = things Ryan may still need to verify. Deterministic. */
function unansweredQuestions(lead: LeadContext): string[] {
  return (lead.acquisitionPlan ?? []).map((t) => t.question);
}

function callSummary(session: SessionState, profile: ProspectProfile): string {
  const parts: string[] = [];
  if (profile.coverage_purpose !== UNKNOWN) parts.push(`Interested in ${profile.coverage_purpose} coverage`);
  if (profile.beneficiary_relationship !== UNKNOWN) parts.push(`to protect their ${profile.beneficiary_relationship}`);
  if (profile.desired_coverage_amount) parts.push(`wants about $${profile.desired_coverage_amount.toLocaleString()}`);
  else if (profile.monthly_budget) parts.push(`aiming for about $${profile.monthly_budget}/mo`);
  if (profile.quote_options.length > 0) {
    parts.push(`presented ${profile.quote_options.length} option(s)`);
    if (profile.selected_option) parts.push(`chose Option ${profile.selected_option}`);
  } else if (profile.quoted_monthly_premium) {
    const kind = profile.carrier_name ? profile.carrier_name : "an estimate";
    parts.push(`quoted ~$${profile.quoted_monthly_premium}/mo (${kind})`);
  }
  if (profile.quote_failure_reason) parts.push("quote service failed — manual quote needed");
  if (session.appointment) parts.push("appointment booked");
  return parts.length ? parts.join("; ") + "." : UNKNOWN;
}

/** Normalise a captured rating sex to the only values the record may hold. */
function ratingSexValue(session: SessionState): string {
  const v = String(session.captured[CAPTURE.ratingSex] ?? "").trim().toLowerCase();
  if (v === "male" || v === "female") return v;
  return UNKNOWN;
}

export function buildProspectProfile(
  session: SessionState,
  opts: { appointmentTime: string; followUpNotes?: string }
): ProspectProfile {
  const lead = session.lead;
  const c = session.captured;
  const quote = session.quote;
  const name = [lead.firstName, lead.lastName].filter(Boolean).join(" ").trim();
  const options = session.quoteOptions ?? [];

  const profile: ProspectProfile = {
    lead_id: lead.leadId,
    full_name: name || UNKNOWN,
    phone_e164: lead.phoneE164,
    age: lead.age ?? null,
    date_of_birth: str(c["dob"]),
    state: lead.state ?? UNKNOWN,
    rating_sex: ratingSexValue(session),
    employment_status: str(c[CAPTURE.employmentStatus]),
    height_weight: str(c[CAPTURE.heightWeight]),
    prospect_email: str(c[CAPTURE.prospectEmail]),
    coverage_purpose: str(c[CAPTURE.coveragePurpose]),
    burial_or_cremation_preference: str(c[CAPTURE.burialOrCremation]),
    beneficiary_name: str(c[CAPTURE.beneficiaryName]),
    beneficiary_relationship: str(c[CAPTURE.beneficiaryRelationship]),
    people_protected: str(c[CAPTURE.peopleProtected]),
    desired_coverage_amount: toAmount(c[CAPTURE.desiredCoverageAmount]),
    monthly_budget: toAmount(c[CAPTURE.monthlyBudget]),
    existing_coverage: str(c[CAPTURE.existingCoverage]),
    existing_coverage_amount: toAmount(c[CAPTURE.existingCoverageAmount]),
    motivation_summary: str(c[CAPTURE.motivation]),
    health_summary: str(c[CAPTURE.conditions]),
    medications_summary: str(c[CAPTURE.medications]),
    tobacco_status: str(c[CAPTURE.tobacco]),
    quote_amount: quote?.faceValue ?? null,
    quoted_monthly_premium: quote?.monthlyPremium ?? null,
    quote_type: quote?.quoteType ?? UNKNOWN,
    quote_source: quote ? (quote.quoteType === "carrier" ? "carrier-backed" : "estimate") : UNKNOWN,
    face_amount_reason: quote?.faceAmountReason ?? UNKNOWN,
    carrier_name: quote?.carrierRate ? quote.carrier ?? null : null,
    quote_options: options.map((o) => ({
      label: o.label,
      face_value: o.faceValue,
      monthly_premium: o.monthlyPremium,
      carrier: o.carrierRate ? o.carrier : null,
      quote_type: o.quoteType,
      within_budget: o.withinBudget,
    })),
    selected_option: session.selectedQuoteOption,
    affordability_confirmed: session.affordabilityConfirmed,
    quote_failure_reason: session.quoteFailureReason,
    calendly_status: session.calendly?.status ?? "not_requested",
    appointment_time: opts.appointmentTime || UNKNOWN,
    appointment_timezone: session.calendly?.timezone ?? UNKNOWN,
    objections: session.objections,
    consent_status: lead.consentStatus ?? UNKNOWN,
    follow_up_notes: opts.followUpNotes?.trim() || UNKNOWN,
    transcript_summary: UNKNOWN,
    risk_flags: riskFlags(session),
    unanswered_questions: unansweredQuestions(lead),
  };
  profile.transcript_summary = callSummary(session, profile);
  return profile;
}
