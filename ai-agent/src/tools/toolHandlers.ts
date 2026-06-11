import type { Actor } from "xstate";
import { z } from "zod";
import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import { loadConfig } from "../config.js";
import { logger, toolInvocations } from "../observability.js";
import { PolicyEngine } from "../policy/policyEngine.js";
import type { LeadContext } from "../types.js";
import type { SalesContext, SalesEvent } from "../machine/salesMachine.js";
import { randomUUID } from "crypto";

const policy = new PolicyEngine();

type ToolInput = Record<string, unknown>;

export type ToolHandler = (
  input: ToolInput,
  ctx: { lead: LeadContext; sessionId: string; bridge: KafkaBridge; actor: Actor<any> }
) => Promise<Record<string, unknown>>;

export const toolHandlers: Record<string, ToolHandler> = {
  get_quote: async (input, { lead, sessionId, bridge, actor }) => {
    const parsed = z
      .object({
        productLine: z.string(),
        age: z.number().int(),
        state: z.string(),
        faceValue: z.number().int().optional(),
      })
      .parse(input);

    const correlationId = randomUUID();
    let quote;
    try {
      quote = await bridge.requestQuote({
        correlationId,
        leadId: lead.leadId,
        sessionId,
        productLine: parsed.productLine,
        age: parsed.age,
        state: parsed.state,
        faceValue: parsed.faceValue,
      });
    } catch (err) {
      logger.error({ err, correlationId }, "quote request failed");
      toolInvocations.inc({ tool_name: "get_quote", outcome: "timeout" });
      return { error: "Quote service unavailable — please try again in a moment." };
    }

    const policyCheck = policy.evaluateQuote(quote);
    if (!policyCheck.allowed) {
      toolInvocations.inc({ tool_name: "get_quote", outcome: "policy_veto" });
      return { error: policyCheck.reason };
    }

    await bridge.publishEvent({
      eventType: "quote_presented",
      leadId: lead.leadId,
      sessionId,
      quote,
    });

    actor.send({ type: "QUOTE_READY", quote } as Parameters<typeof actor.send>[0]);
    toolInvocations.inc({ tool_name: "get_quote", outcome: "success" });

    return {
      productLine: quote.productLine,
      monthlyPremium: quote.monthlyPremium,
      faceValue: quote.faceValue,
      carrier: quote.carrier,
    };
  },

  book_appointment: async (input, { lead, sessionId, bridge, actor }) => {
    const parsed = z
      .object({
        preferredDate: z.string(),
        preferredTime: z.string(),
        notes: z.string().optional(),
      })
      .parse(input);

    await bridge.publishEvent({
      eventType: "appointment_booked",
      leadId: lead.leadId,
      sessionId,
      slotId: crypto.randomUUID(),
    });

    actor.send({ type: "WRAP_UP" });
    toolInvocations.inc({ tool_name: "book_appointment", outcome: "success" });

    return { booked: true, date: parsed.preferredDate, time: parsed.preferredTime };
  },

  capture_objection: async (input, { lead, sessionId, bridge, actor }) => {
    const parsed = z
      .object({ objectionType: z.string(), text: z.string() })
      .parse(input);

    await bridge.publishEvent({
      eventType: "objection_captured",
      leadId: lead.leadId,
      sessionId,
      objectionType: parsed.objectionType,
      text: parsed.text,
    });

    actor.send({ type: "OBJECTION" });
    toolInvocations.inc({ tool_name: "capture_objection", outcome: "success" });

    return { captured: true };
  },

  capture_consent: async (input, { lead, sessionId, bridge }) => {
    const parsed = z
      .object({
        consentType: z.enum([
          "contact_consent",
          "recording_consent",
          "sms_consent",
          "application_consent",
          "health_disclosure_ack",
        ]),
        consentText: z.string(),
        timestamp: z.string().optional(),
      })
      .parse(input);

    await bridge.publishEvent({
      eventType: "consent_captured",
      leadId: lead.leadId,
      sessionId,
      consentType: parsed.consentType,
      evidence: {
        consentText: parsed.consentText,
        timestamp: parsed.timestamp ?? new Date().toISOString(),
      },
    });

    toolInvocations.inc({ tool_name: "capture_consent", outcome: "success" });
    return { captured: true, consentType: parsed.consentType };
  },

  capture_dnc: async (input, { lead, sessionId, bridge, actor }) => {
    const parsed = z
      .object({ phoneE164: z.string(), reason: z.string().optional() })
      .parse(input);

    await bridge.publishEvent({
      eventType: "dnc_request",
      leadId: lead.leadId,
      sessionId,
      phoneE164: parsed.phoneE164,
    });

    actor.send({ type: "DNC" });
    toolInvocations.inc({ tool_name: "capture_dnc", outcome: "success" });

    return { registered: true };
  },

  transfer_live: async (input, { lead, sessionId, bridge, actor }) => {
    const parsed = z
      .object({
        reason: z.string().optional(),
        topCarrier: z.string().optional(),
        topTier: z.string().optional(),
        quoteAmount: z.number().optional(),
      })
      .parse(input);

    await bridge.publishEvent({
      eventType: "live_transfer_requested",
      leadId: lead.leadId,
      sessionId,
      reason: parsed.reason,
      topCarrier: parsed.topCarrier,
      topTier: parsed.topTier,
      quoteAmount: parsed.quoteAmount,
    });

    actor.send({ type: "TRANSFER" });
    toolInvocations.inc({ tool_name: "transfer_live", outcome: "success" });

    return { bridging: true, message: "Connecting you with a specialist right now — one moment." };
  },

  transfer_to_human: async (input, { lead, sessionId, bridge, actor }) => {
    const parsed = z
      .object({ reason: z.string().optional(), priority: z.string().optional() })
      .parse(input);

    await bridge.publishEvent({
      eventType: "transferred",
      leadId: lead.leadId,
      sessionId,
      reason: parsed.reason,
    });

    actor.send({ type: "TRANSFER" });
    toolInvocations.inc({ tool_name: "transfer_to_human", outcome: "success" });

    return { transferred: true, reason: parsed.reason };
  },

  end_call: async (input, { lead, sessionId, bridge, actor }) => {
    const parsed = z
      .object({ outcome: z.string().optional(), notes: z.string().optional() })
      .parse(input);

    await bridge.publishEvent({
      eventType: "call_ended",
      leadId: lead.leadId,
      sessionId,
      outcome: parsed.outcome,
    });

    actor.send({ type: "END" });
    toolInvocations.inc({ tool_name: "end_call", outcome: "success" });

    return { ended: true, outcome: parsed.outcome };
  },

  update_lead_field: async (input, { lead, sessionId, bridge }) => {
    const parsed = z
      .object({
        field: z.string(),
        captureKey: z.string(),
        value: z.unknown(),
        rawText: z.string().optional(),
      })
      .parse(input);

    await bridge.publishEvent({
      eventType: "field_captured",
      leadId: lead.leadId,
      sessionId,
      field: parsed.field,
      captureKey: parsed.captureKey,
      value: parsed.value,
    });

    // Remove from the local acquisition plan so pushSessionUpdate reflects progress
    if (lead.acquisitionPlan) {
      lead.acquisitionPlan = lead.acquisitionPlan.filter(
        (t) => t.field !== parsed.field
      );
    }
    // Sync age into the lead context for immediate use (e.g. get_quote)
    if (parsed.field === "age" && typeof parsed.value === "number") {
      lead.age = parsed.value;
    }

    toolInvocations.inc({ tool_name: "update_lead_field", outcome: "success" });

    const remaining = lead.acquisitionPlan?.length ?? 0;
    return {
      captured: true,
      field: parsed.field,
      remainingFields: remaining,
      nextQuestion: remaining > 0 ? (lead.acquisitionPlan?.[0]?.question ?? null) : null,
    };
  },
};
