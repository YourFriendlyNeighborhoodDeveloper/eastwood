import type { Actor } from "xstate";
import { z } from "zod";
import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import { loadConfig } from "../config.js";
import { logger, toolInvocations } from "../observability.js";
import { PolicyEngine } from "../policy/policyEngine.js";
import type { AcquisitionTask, LeadContext, ProductLine, SessionState } from "../types.js";
import type { SalesContext, SalesEvent } from "../machine/salesMachine.js";
import { randomUUID } from "crypto";

const policy = new PolicyEngine();

type ToolInput = Record<string, unknown>;

export type ToolHandler = (
  input: ToolInput,
  ctx: {
    lead: LeadContext;
    sessionId: string;
    bridge: KafkaBridge;
    actor: Actor<any>;
    session: SessionState;
  }
) => Promise<Record<string, unknown>>;

export const toolHandlers: Record<string, ToolHandler> = {
  get_quote: async (input, { lead, sessionId, bridge, actor, session }) => {
    const parsed = z
      .object({
        productLine: z.string(),
        age: z.number().int(),
        state: z.string(),
        faceValue: z.number().int().optional(),
        tobaccoUser: z.boolean().optional(),
        gender: z.string().optional(),
      })
      .parse(input);

    const cfg = loadConfig();
    let quoteData: Record<string, unknown>;
    try {
      const resp = await fetch(`${cfg.eastwoodCoreBaseUrl}/api/quote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          product_line: parsed.productLine,
          age: parsed.age,
          state: parsed.state,
          face_value: parsed.faceValue ?? 10_000,
          tobacco: parsed.tobaccoUser ?? false,
          gender: parsed.gender ?? null,
          lead_id: lead.leadId,
          condition_codes: [],
        }),
      });
      if (!resp.ok) throw new Error(`quote API ${resp.status}`);
      quoteData = (await resp.json()) as Record<string, unknown>;
    } catch (err) {
      logger.error({ err }, "get_quote HTTP call failed");
      toolInvocations.inc({ tool_name: "get_quote", outcome: "timeout" });
      return { error: "Quote service unavailable — please try again in a moment." };
    }

    const best = quoteData["best"] as Record<string, unknown> | null;
    const quote = {
      productLine: parsed.productLine as ProductLine,
      monthlyPremium: (best?.["monthly_premium"] as number) ?? 99.0,
      carrier: (best?.["carrier"] as string) ?? "Eastwood Preferred",
      faceValue: parsed.faceValue ?? 10_000,
    };

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

    // Source of truth for tool gating. The machine also receives QUOTE_READY, but
    // it only handles that event in the `quoting` state, which nothing reaches.
    session.quote = quote;

    actor.send({ type: "QUOTE_READY", quote } as Parameters<typeof actor.send>[0]);
    toolInvocations.inc({ tool_name: "get_quote", outcome: "success" });

    const allOptions = (quoteData["all_options"] as unknown[])?.slice(0, 3) ?? [];
    return {
      productLine: quote.productLine,
      monthlyPremium: quote.monthlyPremium,
      faceValue: quote.faceValue,
      carrier: quote.carrier,
      source: quoteData["quote_source"] ?? "formula",
      topOptions: allOptions,
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

    const cfg = loadConfig();
    let appointmentId: string = randomUUID();
    try {
      const resp = await fetch(`${cfg.eastwoodCoreBaseUrl}/api/appointments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lead_id: lead.leadId,
          session_id: sessionId,
          preferred_date: parsed.preferredDate,
          preferred_time: parsed.preferredTime,
          notes: parsed.notes,
        }),
      });
      if (resp.ok) {
        const data = (await resp.json()) as Record<string, unknown>;
        if (typeof data["appointment_id"] === "string") {
          appointmentId = data["appointment_id"];
        }
      } else {
        logger.warn({ status: resp.status }, "appointments API returned non-OK; falling back to Kafka-only");
      }
    } catch (err) {
      logger.error({ err }, "book_appointment HTTP call failed; falling back to Kafka-only");
    }

    // Always publish the event — brain.py sends the SMS confirmation
    await bridge.publishEvent({
      eventType: "appointment_booked",
      leadId: lead.leadId,
      sessionId,
      slotId: appointmentId,
    });

    actor.send({ type: "WRAP_UP" });
    toolInvocations.inc({ tool_name: "book_appointment", outcome: "success" });

    return {
      booked: true,
      appointmentId,
      date: parsed.preferredDate,
      time: parsed.preferredTime,
      message: `Your appointment is confirmed for ${parsed.preferredDate} at ${parsed.preferredTime}. You'll receive a confirmation text shortly.`,
    };
  },

  add_followup_questions: async (input, { lead, sessionId }) => {
    const parsed = z.object({ condition: z.string() }).parse(input);
    const cfg = loadConfig();

    let questions: AcquisitionTask[] = [];
    try {
      const resp = await fetch(`${cfg.eastwoodCoreBaseUrl}/api/agenda/followups`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lead_id: lead.leadId, condition: parsed.condition }),
      });
      if (resp.ok) {
        const data = (await resp.json()) as { questions: AcquisitionTask[] };
        questions = data.questions ?? [];
      } else {
        logger.warn({ status: resp.status, condition: parsed.condition }, "agenda/followups returned non-OK");
      }
    } catch (err) {
      logger.error({ err }, "add_followup_questions HTTP call failed");
    }

    if (questions.length > 0) {
      lead.acquisitionPlan = [...(lead.acquisitionPlan ?? []), ...questions];
    }

    toolInvocations.inc({ tool_name: "add_followup_questions", outcome: "success" });
    logger.info({ sessionId, condition: parsed.condition, added: questions.length }, "followup questions added");

    return {
      condition: parsed.condition,
      questionsAdded: questions.length,
      nextQuestion: questions[0]?.question ?? null,
    };
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
