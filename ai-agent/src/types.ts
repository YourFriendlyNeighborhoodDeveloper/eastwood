import { z } from "zod";

import type { SlotRecord } from "./discovery/slotState.js";
import type { QuestionExpectation, TurnOutcome } from "./discovery/turnGate.js";

export const ProductLineSchema = z.enum([
  "final_expense",
  "medicare_advantage",
  "medicare_supplement",
  "aca",
  "annuity",
  "life",
]);
export type ProductLine = z.infer<typeof ProductLineSchema>;

export const ConsentStatusSchema = z.enum([
  "express_written",
  "implied",
  "none",
  "revoked",
]);
export type ConsentStatus = z.infer<typeof ConsentStatusSchema>;

export const AcquisitionTaskSchema = z.object({
  field: z.string(),
  priority: z.number().int(),
  question: z.string(),
  captureKey: z.string(),
  valueHint: z.string().optional(),
});
export type AcquisitionTask = z.infer<typeof AcquisitionTaskSchema>;

export const LeadContextSchema = z.object({
  leadId: z.string().uuid(),
  tenantId: z.string(),
  phoneE164: z.string(),
  // .nullable() alongside .optional() on every field below: eastwood-core's
  // ai_agent.py always includes these keys, so an unset Python field arrives
  // as JSON null, not an absent key. Zod's .optional() alone rejects null —
  // that mismatch made session creation 500 for any lead missing, e.g., a
  // last name or triage bucket (i.e. most real leads), so the call never
  // started at all. Found via a live test call, not a hypothetical.
  firstName: z.string().nullable().optional(),
  lastName: z.string().nullable().optional(),
  age: z.number().int().nullable().optional(),
  state: z.string().length(2).nullable().optional(),
  productLine: ProductLineSchema.nullable().optional(),
  consentStatus: ConsentStatusSchema.nullable().optional(),
  score: z.number().int().min(0).max(100).nullable().optional(),
  enrichment: z.record(z.unknown()).nullable().optional(),
  triageBucket: z.string().nullable().optional(),
  acquisitionPlan: z.array(AcquisitionTaskSchema).nullable().optional(),
});
export type LeadContext = z.infer<typeof LeadContextSchema>;

/** Why the quoted face amount was chosen. agent_default is refused in production. */
export const FaceAmountReasonSchema = z.enum([
  "prospect_requested",
  "budget_fit",
  "needs_estimate",
  "agent_default",
]);
export type FaceAmountReason = z.infer<typeof FaceAmountReasonSchema>;

export const QuoteSchema = z.object({
  productLine: ProductLineSchema,
  faceValue: z.number().int().positive().optional(),
  monthlyPremium: z.number().positive(),
  carrier: z.string().optional(),
  termYears: z.number().int().positive().optional(),
  // Honesty labels from eastwood-core. Defaults are the safe reading: a payload
  // that omits them is an estimate that binds nothing.
  quoteType: z.enum(["estimated", "carrier"]).default("estimated"),
  rateSource: z.string().default("unknown"),
  carrierRate: z.boolean().default(false),
  bindable: z.boolean().default(false),
  faceAmountReason: FaceAmountReasonSchema.default("agent_default"),
});
export type Quote = z.infer<typeof QuoteSchema>;

/**
 * One of the three deterministic options (C/B/A) sized and priced by
 * eastwood-core's quote engine. The model presents them; it never invents them.
 */
export const QuoteOptionSchema = z.object({
  label: z.enum(["C", "B", "A"]),
  faceValue: z.number().int().positive(),
  monthlyPremium: z.number().positive(),
  carrier: z.string().nullable().default(null),
  quoteType: z.enum(["estimated", "carrier"]).default("estimated"),
  rateSource: z.string().default("unknown"),
  carrierRate: z.boolean().default(false),
  bindable: z.boolean().default(false),
  /** null when no budget was stated; false means it may only be explored with permission. */
  withinBudget: z.boolean().nullable().default(null),
});
export type QuoteOption = z.infer<typeof QuoteOptionSchema>;

/** Calendly booking state for the session — persisted into the profile. */
export interface CalendlyState {
  status: "confirmed" | "failed" | "not_requested";
  eventUri: string | null;
  inviteeUri: string | null;
  startTime: string | null;
  timezone: string | null;
  cancelUrl: string | null;
  rescheduleUrl: string | null;
}

export const ObjectionSchema = z.object({
  objectionType: z.string(),
  text: z.string(),
});
export type Objection = z.infer<typeof ObjectionSchema>;

/** Marker for a field the prospect never gave us. Never guessed or invented. */
export const UNKNOWN = "unknown" as const;

/**
 * The full handoff picture assembled from captured facts — not a model-authored
 * summary. Every field is either something the prospect stated (via
 * update_lead_field / capture_objection / get_quote) or explicitly "unknown".
 */
export interface ProspectProfile {
  lead_id: string;
  full_name: string;
  phone_e164: string;
  age: number | null;
  date_of_birth: string;
  state: string;
  /** "male" | "female" | "unknown" — always asked, never inferred from name or voice. */
  rating_sex: string;
  employment_status: string;
  height_weight: string;
  prospect_email: string;
  coverage_purpose: string;
  burial_or_cremation_preference: string;
  beneficiary_name: string;
  beneficiary_relationship: string;
  people_protected: string;
  desired_coverage_amount: number | null;
  monthly_budget: number | null;
  existing_coverage: string;
  existing_coverage_amount: number | null;
  motivation_summary: string;
  health_summary: string;
  medications_summary: string;
  tobacco_status: string;
  quote_amount: number | null;
  quoted_monthly_premium: number | null;
  quote_type: string;
  quote_source: string;
  face_amount_reason: string;
  carrier_name: string | null;
  /** The three engine-priced options as presented, in C/B/A order. */
  quote_options: Array<{
    label: string;
    face_value: number;
    monthly_premium: number;
    carrier: string | null;
    quote_type: string;
    within_budget: boolean | null;
  }>;
  selected_option: string | null;
  affordability_confirmed: boolean | null;
  quote_failure_reason: string | null;
  calendly_status: string;
  appointment_time: string;
  appointment_timezone: string;
  objections: Array<{ objectionType: string; text: string }>;
  consent_status: string;
  follow_up_notes: string;
  transcript_summary: string;
  risk_flags: string[];
  unanswered_questions: string[];
  /** Fields stored without a verified transcript behind them, or only after
   *  the prospect confirmed a poorly-heard value on read-back. Normally
   *  empty. A licensed agent should re-confirm anything listed here before
   *  relying on it — this is the difference between a record that is accurate
   *  and one that is merely complete. */
  unverified_fields: string[];
}

/**
 * Mutable per-call state that tool handlers read and write. Deliberately narrow:
 * RealtimeHandler's ActiveSession satisfies it structurally, so handlers can
 * record a quote without importing the handler (and creating a cycle).
 */
export interface SessionState {
  lead: LeadContext;
  quote: Quote | null;
  /** The three engine-priced options (C/B/A order) once get_quote succeeds. */
  quoteOptions: QuoteOption[] | null;
  /** Which option the prospect chose ("A" | "B" | "C"), recorded via select_quote_option. */
  selectedQuoteOption: string | null;
  /** Prospect confirmed the chosen premium is comfortable to maintain long term. */
  affordabilityConfirmed: boolean | null;
  /** Set when get_quote failed so booking/transfer can proceed honestly for manual quoting. */
  quoteFailureReason: string | null;
  /** Real availability retrieved from core before any time was offered. */
  offeredSlots: string[];
  /** Calendly booking result for this call, if any. */
  calendly: CalendlyState | null;
  appointmentBooked: boolean;
  /** Fields captured from the prospect, keyed by captureKey. Source of the profile. */
  captured: Record<string, unknown>;
  /** Finalized transcripts of the PROSPECT's own speech, in order. The
   *  evidence base a captured slot must be traceable to — a value that
   *  matches nothing here was never actually said. `confidence` is the mean
   *  per-token probability from transcription logprobs, or null when the API
   *  sent none (unscored — which is not the same as confident). */
  userTranscripts: Array<{ text: string; at: number; confidence?: number | null }>;
  /** Provenance for every stored slot: the prospect's own words, when it was
   *  captured, how sure the transcriber was, and whether it was verified
   *  against a transcript. */
  captureEvidence: Record<
    string,
    {
      heardVerbatim: string;
      at: number;
      grounding: "grounded" | "unverifiable";
      /** Mean per-token probability of the supporting transcript, or null. */
      confidence?: number | null;
      /** Transcript excerpt the value was traced to. */
      matched?: string;
    }
  >;
  /** When the prospect's most recent turn of speech ended. A capture made
   *  after this must be judged against a transcript of THAT turn, not merely
   *  whatever had arrived earlier. Null before the prospect has spoken. */
  lastSpeechEndedAt?: number | null;
  /** True once any transcript has arrived on this call — i.e. transcription
   *  is demonstrably working. Distinguishes "this turn produced no words"
   *  (suspicious) from "transcription is not running at all" (degraded, and
   *  not the model's fault). */
  transcriptionEverSeen?: boolean;
  /** Waits until a transcript finalized at/after `sinceMs` is available, or
   *  until `timeoutMs` elapses. Supplied by the realtime handler; absent in
   *  unit tests, where transcripts are seeded up front. */
  waitForTranscriptSince?: (sinceMs: number, timeoutMs: number) => Promise<void>;
  /** Every required slot's position in UNKNOWN → HEARD → CONFIRMED. Only a
   *  CONFIRMED slot has a value, and only a CONFIRMED slot appears in
   *  `captured`. A HEARD slot holds a candidate awaiting read-back. */
  slots?: Record<string, SlotRecord>;
  /** The turn gate's ruling on the prospect's most recent turn. A capture
   *  claimed from a turn where nothing usable was heard is refused whatever
   *  the model reports, so noise can never reach a slot by a second route. */
  turnVerdict?: {
    outcome: TurnOutcome;
    reason: string;
    /** What the assistant's last question was looking for. */
    expectation: QuestionExpectation;
    at: number;
  } | null;
  /** The assistant's last spoken question and the answer shape it implies —
   *  read from what she actually said out loud, not from the agenda. */
  pendingQuestion?: { text: string; expectation: QuestionExpectation; at: number } | null;
  /** Objections raised during the call, in order. */
  objections: Array<{ objectionType: string; text: string }>;
  /** Set once book_appointment succeeds, so a repeat call is a no-op (no second POST/SMS). */
  appointment: { appointmentId: string; date: string; time: string } | null;
  /** Set once the prospect has asked not to be called. Revokes every selling tool. */
  dncCaptured: boolean;
  /** Set once call_ended has been published, so endSession does not emit a second. */
  callEndedPublished: boolean;
}

export const AgentEventSchema = z.discriminatedUnion("eventType", [
  z.object({
    eventType: z.literal("quote_presented"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    quote: QuoteSchema,
    /** The full C/B/A options set presented, when the engine produced one. */
    options: z.array(QuoteOptionSchema).optional(),
  }),
  z.object({
    eventType: z.literal("objection_captured"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    objectionType: z.string(),
    text: z.string().optional(),
  }),
  z.object({
    eventType: z.literal("consent_captured"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    consentType: z.enum([
      "contact_consent",
      "recording_consent",
      "sms_consent",
      "application_consent",
      "health_disclosure_ack",
    ]),
    evidence: z.record(z.unknown()).optional(),
  }),
  z.object({
    eventType: z.literal("dnc_request"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    phoneE164: z.string(),
  }),
  z.object({
    eventType: z.literal("appointment_booked"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    slotId: z.string().uuid(),
  }),
  z.object({
    eventType: z.literal("call_ended"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    outcome: z.string().optional(),
    durationSeconds: z.number().int().optional(),
    /** Full handoff profile for every connected call — drives Ryan's outcome
     *  SMS in eastwood-core. Built from captured facts, never model-authored. */
    prospectProfile: z.record(z.unknown()).optional(),
  }),
  z.object({
    eventType: z.literal("transferred"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    reason: z.string().optional(),
  }),
  z.object({
    eventType: z.literal("field_captured"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    field: z.string(),
    captureKey: z.string(),
    value: z.unknown(),
  }),
  z.object({
    eventType: z.literal("live_transfer_requested"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    reason: z.string().optional(),
    topCarrier: z.string().optional(),
    topTier: z.string().optional(),
    quoteAmount: z.number().optional(),
  }),
]);
export type AgentEvent = z.infer<typeof AgentEventSchema>;

export const InboundCommandSchema = z.discriminatedUnion("command", [
  z.object({
    command: z.literal("start_session"),
    leadId: z.string().uuid(),
    context: LeadContextSchema,
  }),
  z.object({
    command: z.literal("end_session"),
    sessionId: z.string(),
    reason: z.string().optional(),
  }),
]);
export type InboundCommand = z.infer<typeof InboundCommandSchema>;

export const PolicyDecisionSchema = z.object({
  allowed: z.boolean(),
  reason: z.string(),
  severity: z.enum(["info", "warn", "block"]),
});
export type PolicyDecision = z.infer<typeof PolicyDecisionSchema>;
