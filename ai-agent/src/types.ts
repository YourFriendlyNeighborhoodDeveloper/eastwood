import { z } from "zod";

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
  firstName: z.string().optional(),
  lastName: z.string().optional(),
  age: z.number().int().optional(),
  state: z.string().length(2).optional(),
  productLine: ProductLineSchema.optional(),
  consentStatus: ConsentStatusSchema.optional(),
  score: z.number().int().min(0).max(100).optional(),
  enrichment: z.record(z.unknown()).optional(),
  triageBucket: z.string().optional(),
  acquisitionPlan: z.array(AcquisitionTaskSchema).optional(),
});
export type LeadContext = z.infer<typeof LeadContextSchema>;

export const QuoteSchema = z.object({
  productLine: ProductLineSchema,
  faceValue: z.number().int().positive().optional(),
  monthlyPremium: z.number().positive(),
  carrier: z.string().optional(),
  termYears: z.number().int().positive().optional(),
});
export type Quote = z.infer<typeof QuoteSchema>;

export const ObjectionSchema = z.object({
  objectionType: z.string(),
  text: z.string(),
});
export type Objection = z.infer<typeof ObjectionSchema>;

/**
 * Mutable per-call state that tool handlers read and write. Deliberately narrow:
 * RealtimeHandler's ActiveSession satisfies it structurally, so handlers can
 * record a quote without importing the handler (and creating a cycle).
 */
export interface SessionState {
  quote: Quote | null;
  appointmentBooked: boolean;
  /** Set once call_ended has been published, so endSession does not emit a second. */
  callEndedPublished: boolean;
}

export const AgentEventSchema = z.discriminatedUnion("eventType", [
  z.object({
    eventType: z.literal("quote_presented"),
    leadId: z.string().uuid(),
    sessionId: z.string(),
    quote: QuoteSchema,
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
