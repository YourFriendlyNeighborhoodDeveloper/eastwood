import type { Actor } from "xstate";
import { z } from "zod";
import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import { loadConfig } from "../config.js";
import { coreFetch } from "../coreClient.js";
import { logger, toolInvocations } from "../observability.js";
import { PolicyEngine } from "../policy/policyEngine.js";
import type { AcquisitionTask, LeadContext, ProductLine, Quote, QuoteOption, SessionState } from "../types.js";
import { QuoteOptionSchema } from "../types.js";
import type { SalesContext, SalesEvent } from "../machine/salesMachine.js";
import {
  CAPTURE,
  buildProspectProfile,
  missingForQuote,
  quoteUnlocked,
  selectFaceAmount,
} from "../discovery/prospectProfile.js";

const policy = new PolicyEngine();

/**
 * The only sentence Imogen may use to present a formula estimate.
 *
 * It names no carrier, claims no final rate, and routes to a licensed review.
 * A formula premium is a curve fitted to mid-market benchmarks — no carrier has
 * quoted it and none is obliged to honour it.
 */
export function estimatePresentation(monthlyPremium: number): string {
  return (
    `Based on what you told me, we may be looking around $${monthlyPremium.toFixed(2)} per month. ` +
    `I'd like to book a licensed review to confirm the exact carrier and final rate.`
  );
}

const OPTION_FRAMING: Record<string, string> = {
  C: "This is the basic protection option — it handles the minimum need you described.",
  B: "This is the balanced option — it covers the need with some room for inflation or extra expenses.",
  A: "This gives your beneficiary the strongest protection while staying near the budget we discussed.",
};

function money(n: number): string {
  return `$${n.toLocaleString("en-US")}`;
}

/**
 * The exact lines for presenting the engine's options. Each option is a
 * specific face amount at a specific premium — never a vague range. Estimates
 * are said to be estimates with the carrier TBD; only carrier-backed rates may
 * name their carrier.
 */
export function optionsPresentation(options: QuoteOption[]): string[] {
  const lines: string[] = [
    options.length > 1
      ? `Based on what you told me, I found ${options.length === 3 ? "three" : options.length === 2 ? "two" : "a few"} ways to approach it.`
      : "Based on what you told me, here is the option I found.",
  ];
  for (const o of options) {
    const framing = OPTION_FRAMING[o.label] ?? "";
    lines.push(
      `Option ${o.label} is ${money(o.faceValue)} of coverage at approximately $${o.monthlyPremium.toFixed(2)} per month. ${framing}`.trim()
    );
  }
  const anyEstimate = options.some((o) => o.quoteType !== "carrier");
  if (anyEstimate) {
    lines.push(
      "Those figures are estimates — the exact carrier and final rate get confirmed by the licensed agent."
    );
  }
  if (options.length > 1) {
    lines.push("Which of those feels like the best fit for what you're trying to accomplish?");
  }
  return lines;
}

/** Map an engine option onto the session's Quote record. */
function optionToQuote(
  productLine: ProductLine,
  option: QuoteOption,
  faceAmountReason: Quote["faceAmountReason"]
): Quote {
  return {
    productLine,
    faceValue: option.faceValue,
    monthlyPremium: option.monthlyPremium,
    carrier: option.carrier ?? undefined,
    quoteType: option.quoteType,
    rateSource: option.rateSource,
    carrierRate: option.carrierRate,
    bindable: option.bindable,
    faceAmountReason,
  };
}

/**
 * The appointment time string for the handoff profile — the Calendly slot when
 * one was booked, otherwise the locally recorded preference.
 */
function appointmentTimeString(session: SessionState): string {
  if (session.calendly?.startTime) {
    return [session.calendly.startTime, session.calendly.timezone].filter(Boolean).join(" ");
  }
  if (session.appointment) {
    return `${session.appointment.date} ${session.appointment.time}`.trim();
  }
  return "";
}

/** Handoff profile for call_ended — built from captured facts only. */
export function handoffProfile(session: SessionState, notes?: string): Record<string, unknown> {
  return buildProspectProfile(session, {
    appointmentTime: appointmentTimeString(session),
    followUpNotes: notes,
  }) as unknown as Record<string, unknown>;
}

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
      })
      .parse(input);

    const cfg = loadConfig();

    // The model does not choose the face amount; it is derived from what the
    // prospect actually stated (requested amount, or a budget-fit sizing).
    const age = lead.age ?? parsed.age;
    const { faceValue, reason: faceAmountReason } = selectFaceAmount(session, age);
    if (faceAmountReason === "agent_default" && cfg.appEnv === "production") {
      toolInvocations.inc({ tool_name: "get_quote", outcome: "no_basis" });
      return {
        error:
          "I can get you a real number, but I need to make sure I'm pricing the " +
          "right amount for the right purpose first — can we sort out roughly how " +
          "much coverage you're after, or a monthly budget?",
      };
    }

    // Rating sex is whatever the prospect stated — captured via
    // update_lead_field, never inferred by the model and never a tool argument.
    const ratingSexRaw = String(session.captured[CAPTURE.ratingSex] ?? "").trim().toLowerCase();
    const ratingSex = ratingSexRaw === "male" || ratingSexRaw === "female" ? ratingSexRaw : null;

    const c = session.captured;
    let quoteData: Record<string, unknown>;
    try {
      const resp = await coreFetch(`/api/quote`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          product_line: parsed.productLine,
          age: parsed.age,
          state: parsed.state,
          face_value: faceValue,
          tobacco: parsed.tobaccoUser ?? false,
          gender: ratingSex,
          lead_id: lead.leadId,
          condition_codes: [],
          // The engine — not the model — sizes the C/B/A faces from these.
          coverage_purpose: c[CAPTURE.coveragePurpose] ?? null,
          burial_or_cremation: c[CAPTURE.burialOrCremation] ?? null,
          desired_coverage_amount: c[CAPTURE.desiredCoverageAmount] ?? null,
          monthly_budget: c[CAPTURE.monthlyBudget] ?? null,
        }),
      });
      if (!resp.ok) throw new Error(`quote API ${resp.status}`);
      quoteData = (await resp.json()) as Record<string, unknown>;
    } catch (err) {
      // Honest failure: no premium is invented, the profile is preserved, and
      // booking/transfer unlock so the licensed agent can quote manually.
      logger.error({ err }, "get_quote HTTP call failed");
      session.quoteFailureReason = "quote_service_unavailable";
      toolInvocations.inc({ tool_name: "get_quote", outcome: "timeout" });
      return {
        error:
          "The quoting service couldn't finish just now. Do NOT state any number " +
          "or range. Tell the prospect the licensed agent will run exact numbers, " +
          "and offer to book that appointment or transfer now.",
        quoteFailed: true,
        nextStep: "book_appointment",
      };
    }

    const best = quoteData["best"] as Record<string, unknown> | null;

    // Read the honesty labels from core, preferring the top-level values and
    // falling back to the chosen option. Anything not explicitly "carrier" /
    // true is treated as an estimate that binds nothing.
    const label = (key: string): unknown => quoteData[key] ?? best?.[key];
    const quoteType = label("quote_type") === "carrier" ? "carrier" : "estimated";
    const isEstimate = quoteType === "estimated";
    const rateSource = String(label("rate_source") ?? "unknown");
    const carrierRate = !isEstimate && label("carrier_rate") === true;
    const bindable = !isEstimate && label("bindable") === true;

    // The engine's three options (C, B, A) — deterministic faces and premiums.
    // Options that fail schema validation or the premium sanity band are dropped.
    const rawOptions = Array.isArray(quoteData["options"]) ? (quoteData["options"] as unknown[]) : [];
    const options: QuoteOption[] = [];
    for (const raw of rawOptions) {
      const r = raw as Record<string, unknown>;
      const parsedOpt = QuoteOptionSchema.safeParse({
        label: r["label"],
        faceValue: r["face_value"],
        monthlyPremium: r["monthly_premium"],
        carrier: r["carrier"] ?? null,
        quoteType: r["quote_type"] === "carrier" ? "carrier" : "estimated",
        rateSource: r["rate_source"] ?? "unknown",
        carrierRate: r["carrier_rate"] === true,
        bindable: r["bindable"] === true,
        withinBudget: r["within_budget"] ?? null,
      });
      if (!parsedOpt.success) continue;
      const opt = parsedOpt.data;
      const sane = policy.evaluateQuote(
        optionToQuote(parsed.productLine as ProductLine, opt, faceAmountReason)
      );
      if (sane.allowed) options.push(opt);
    }

    if (options.length === 0 && best === null) {
      session.quoteFailureReason = "no_quotes_available";
      toolInvocations.inc({ tool_name: "get_quote", outcome: "empty" });
      return {
        error:
          "The quoting service returned no options. Do NOT state any number or " +
          "range. Offer to book the licensed agent, who will run exact numbers.",
        quoteFailed: true,
        nextStep: "book_appointment",
      };
    }

    // ── Three-option path ─────────────────────────────────────────────────
    if (options.length > 0) {
      // The balanced middle option anchors the session quote until the
      // prospect chooses; select_quote_option replaces it with their choice.
      const anchor = options.find((o) => o.label === "B") ?? options[options.length - 1]!;
      const quote = optionToQuote(parsed.productLine as ProductLine, anchor, faceAmountReason);

      await bridge.publishEvent({
        eventType: "quote_presented",
        leadId: lead.leadId,
        sessionId,
        quote,
        options,
      });

      session.quote = quote;
      session.quoteOptions = options;
      session.quoteFailureReason = null;

      actor.send({ type: "QUOTE_READY", quote } as Parameters<typeof actor.send>[0]);
      toolInvocations.inc({ tool_name: "get_quote", outcome: "success" });

      const anyEstimate = options.some((o) => o.quoteType !== "carrier");
      const overBudget = options.filter((o) => o.withinBudget === false).map((o) => o.label);
      return {
        options: options.map((o) => ({
          option: o.label,
          faceValue: o.faceValue,
          monthlyPremium: o.monthlyPremium,
          carrier: anyEstimate ? null : o.carrier,
          quoteType: o.quoteType,
          withinBudget: o.withinBudget,
        })),
        presentation: optionsPresentation(options),
        disclosure: anyEstimate
          ? "These premiums are estimates, not carrier rates — carrier is TBD and " +
            "nothing is approved or guaranteed. Present each option exactly: a " +
            "specific coverage amount at a specific monthly premium. NEVER give a " +
            "vague range like '$60 to $80'. Do not name any carrier."
          : "Carrier-published rates, still subject to underwriting and not bound. " +
            "Present each option exactly — never a vague range.",
        overBudgetOptions: overBudget,
        overBudgetNote: overBudget.length
          ? `Option(s) ${overBudget.join(", ")} exceed the stated budget — ask permission before recommending them.`
          : null,
        nextStep:
          "Present C, then B, then A. Ask which fits best, call select_quote_option, " +
          "confirm affordability, then book the appointment.",
      };
    }

    // ── Single-quote fallback (engine produced no tiered options) ─────────
    const quote = {
      productLine: parsed.productLine as ProductLine,
      monthlyPremium: (best?.["monthly_premium"] as number) ?? 99.0,
      carrier: (best?.["carrier"] as string) ?? "Eastwood Preferred",
      faceValue,
      quoteType: quoteType as "estimated" | "carrier",
      rateSource,
      carrierRate,
      bindable,
      faceAmountReason,
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
    session.quoteFailureReason = null;

    actor.send({ type: "QUOTE_READY", quote } as Parameters<typeof actor.send>[0]);
    toolInvocations.inc({ tool_name: "get_quote", outcome: "success" });

    const common = {
      productLine: quote.productLine,
      monthlyPremium: quote.monthlyPremium,
      faceValue: quote.faceValue,
      faceAmountReason,
      quoteType,
      rateSource,
      carrierRate,
      bindable,
    };

    if (isEstimate) {
      // The premium is a fitted curve, and no carrier has been selected. The
      // model is deliberately not given a carrier name it could speak aloud,
      // and is handed the exact sentence to say.
      return {
        ...common,
        carrier: null,
        carrierDetermined: false,
        presentation: estimatePresentation(quote.monthlyPremium),
        disclosure:
          "This is an estimate, not a carrier rate. No carrier has been selected " +
          "and nothing is approved. Say the presentation line as written. Do not " +
          "name a carrier, and never describe this number as final, locked in, " +
          "approved, or guaranteed.",
        nextStep: "book_appointment",
      };
    }

    return {
      ...common,
      carrier: quote.carrier,
      carrierDetermined: true,
      topOptions: (quoteData["all_options"] as unknown[])?.slice(0, 3) ?? [],
      disclosure:
        "Carrier-published rate. Still subject to underwriting and not bound — " +
        "the licensed agent confirms it at the appointment.",
      nextStep: "book_appointment",
    };
  },

  select_quote_option: async (input, { lead, sessionId, bridge, session }) => {
    const parsed = z
      .object({
        option: z.enum(["C", "B", "A"]),
        affordabilityConfirmed: z.boolean().optional(),
      })
      .parse(input);

    const chosen = (session.quoteOptions ?? []).find((o) => o.label === parsed.option);
    if (!chosen) {
      toolInvocations.inc({ tool_name: "select_quote_option", outcome: "unknown_option" });
      const available = (session.quoteOptions ?? []).map((o) => o.label).join(", ") || "none";
      return { error: `Option ${parsed.option} was not presented. Available options: ${available}.` };
    }

    session.selectedQuoteOption = parsed.option;
    if (parsed.affordabilityConfirmed !== undefined) {
      session.affordabilityConfirmed = parsed.affordabilityConfirmed;
    }
    // The chosen option becomes THE quote the booking and handoff carry.
    session.quote = optionToQuote(
      session.quote?.productLine ?? "final_expense",
      chosen,
      session.quote?.faceAmountReason ?? "prospect_requested"
    );

    // Audit trail: the selection is a captured fact, persisted like any field.
    await bridge.publishEvent({
      eventType: "field_captured",
      leadId: lead.leadId,
      sessionId,
      field: "selected_quote_option",
      captureKey: "selected_quote_option",
      value: parsed.option,
    });

    toolInvocations.inc({ tool_name: "select_quote_option", outcome: "success" });
    return {
      selected: parsed.option,
      faceValue: chosen.faceValue,
      monthlyPremium: chosen.monthlyPremium,
      affordabilityConfirmed: session.affordabilityConfirmed,
      nextStep:
        session.affordabilityConfirmed
          ? "Book the licensed-agent appointment."
          : `Confirm affordability: "And approximately $${chosen.monthlyPremium.toFixed(2)} per month ` +
            `would be comfortable enough to maintain long term?" Then call select_quote_option ` +
            `again with affordabilityConfirmed.`,
    };
  },

  get_booking_slots: async (input, { sessionId, session }) => {
    const parsed = z.object({ timezone: z.string().optional() }).parse(input);

    const query = parsed.timezone ? `?timezone=${encodeURIComponent(parsed.timezone)}` : "";

    try {
      const resp = await coreFetch(`/api/scheduling/availability${query}`);
      if (!resp.ok) throw new Error(`availability API ${resp.status}`);
      const data = (await resp.json()) as {
        calendly_enabled?: boolean;
        times?: string[];
        timezone?: string;
      };
      const times = (data.times ?? []).slice(0, 3);
      session.offeredSlots = times;
      toolInvocations.inc({ tool_name: "get_booking_slots", outcome: "success" });
      if (times.length === 0) {
        return {
          calendlyEnabled: data.calendly_enabled ?? false,
          times: [],
          note:
            "No live calendar times available. Ask for their preferred day and " +
            "time and book with preferredDate/preferredTime — do not claim a " +
            "calendar slot is confirmed.",
        };
      }
      return {
        calendlyEnabled: true,
        times,
        timezone: data.timezone,
        note:
          "Offer two or three of these exact times and let the prospect pick. " +
          "Before booking, collect their email address for the calendar invitation.",
      };
    } catch (err) {
      logger.warn({ err, sessionId }, "get_booking_slots failed");
      session.offeredSlots = [];
      toolInvocations.inc({ tool_name: "get_booking_slots", outcome: "error" });
      return {
        calendlyEnabled: false,
        times: [],
        note:
          "Scheduling calendar unavailable. Ask for their preferred day and time " +
          "and book with preferredDate/preferredTime — do not claim a calendar " +
          "slot is confirmed.",
      };
    }
  },

  book_appointment: async (input, { lead, sessionId, actor, session }) => {
    const parsed = z
      .object({
        slotStartTime: z.string().optional(),
        email: z.string().optional(),
        preferredDate: z.string().optional(),
        preferredTime: z.string().optional(),
        timezone: z.string().optional(),
        notes: z.string().optional(),
      })
      .parse(input);

    // Idempotent: a repeated call returns the existing booking without a second
    // POST, so eastwood-core writes one appointment and sends Ryan one brief.
    if (session.appointment) {
      toolInvocations.inc({ tool_name: "book_appointment", outcome: "duplicate" });
      return {
        booked: true,
        appointmentId: session.appointment.appointmentId,
        date: session.appointment.date,
        time: session.appointment.time,
        message: "That appointment is already on the calendar.",
      };
    }

    if (!parsed.slotStartTime && !parsed.preferredDate) {
      return { booked: false, error: "Need a chosen slot (slotStartTime) or a preferredDate." };
    }

    // A Calendly slot must be one of the times actually retrieved and offered —
    // an invented time is never booked.
    if (parsed.slotStartTime) {
      if (session.offeredSlots.length === 0) {
        toolInvocations.inc({ tool_name: "book_appointment", outcome: "no_availability" });
        return {
          booked: false,
          error: "Call get_booking_slots first — only real availability can be offered and booked.",
        };
      }
      if (!session.offeredSlots.includes(parsed.slotStartTime)) {
        toolInvocations.inc({ tool_name: "book_appointment", outcome: "slot_not_offered" });
        return {
          booked: false,
          error: `That time was not in the offered availability. Offered: ${session.offeredSlots.join(", ")}`,
        };
      }
      // The calendar invitation needs the prospect's email — collect it before
      // booking, never invent one.
      const email = parsed.email?.trim() || String(session.captured[CAPTURE.prospectEmail] ?? "").trim();
      if (!email) {
        toolInvocations.inc({ tool_name: "book_appointment", outcome: "needs_email" });
        return {
          booked: false,
          needsEmail: true,
          error:
            "I need the prospect's email address for the calendar invitation. Ask " +
            "for it, record it with update_lead_field (captureKey prospect_email), " +
            "then book again.",
        };
      }
      session.captured[CAPTURE.prospectEmail] = email;
    }

    // Assemble the handoff profile from captured facts before we write, so a
    // successful appointment carries Ryan's brief. Never model-authored.
    const appointmentTime = parsed.slotStartTime
      ? [parsed.slotStartTime, parsed.timezone].filter(Boolean).join(" ")
      : [parsed.preferredDate, parsed.preferredTime, parsed.timezone].filter(Boolean).join(" ");
    const profile = buildProspectProfile(session, {
      appointmentTime,
      followUpNotes: parsed.notes,
    });

    // eastwood-core is the single source of truth: POST /api/appointments writes
    // the row, books the Calendly slot when one was chosen, persists the
    // profile, and sends Ryan the brief. The agent must NOT publish
    // appointment_booked itself.
    let data: Record<string, unknown>;
    try {
      const resp = await coreFetch(`/api/appointments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          lead_id: lead.leadId,
          session_id: sessionId,
          preferred_date: parsed.preferredDate,
          preferred_time: parsed.preferredTime,
          start_time: parsed.slotStartTime,
          email: parsed.email?.trim() || (session.captured[CAPTURE.prospectEmail] as string | undefined),
          timezone: parsed.timezone,
          notes: parsed.notes,
          selected_option: session.selectedQuoteOption,
          prospect_profile: profile,
        }),
      });
      if (!resp.ok) throw new Error(`appointments API ${resp.status}`);
      data = (await resp.json()) as Record<string, unknown>;
    } catch (err) {
      // Never tell the prospect they are booked when nothing was written.
      logger.error({ err, sessionId }, "book_appointment failed");
      toolInvocations.inc({ tool_name: "book_appointment", outcome: "error" });
      return {
        booked: false,
        error: "I couldn't lock that slot in just now — could we try another time?",
      };
    }

    const appointmentId = String(data["appointment_id"] ?? "");
    const calendlyStatus = String(data["calendly_status"] ?? "not_requested");
    const date = String(data["date"] ?? parsed.preferredDate ?? "");
    const time = String(data["time"] ?? parsed.preferredTime ?? "");

    session.appointmentBooked = true;
    session.appointment = { appointmentId, date, time };
    session.calendly = {
      status: (["confirmed", "failed"].includes(calendlyStatus) ? calendlyStatus : "not_requested") as
        | "confirmed"
        | "failed"
        | "not_requested",
      eventUri: (data["calendly_event_uri"] as string | null) ?? null,
      inviteeUri: (data["calendly_invitee_uri"] as string | null) ?? null,
      startTime: parsed.slotStartTime ?? null,
      timezone: (data["timezone"] as string | null) ?? parsed.timezone ?? null,
      cancelUrl: (data["calendly_cancel_url"] as string | null) ?? null,
      rescheduleUrl: (data["calendly_reschedule_url"] as string | null) ?? null,
    };
    actor.send({ type: "WRAP_UP" });
    toolInvocations.inc({ tool_name: "book_appointment", outcome: "success" });

    if (calendlyStatus === "failed") {
      // The request is recorded and Ryan will get the requested time, but the
      // calendar booking did NOT complete — the prospect must not be told the
      // appointment is confirmed.
      return {
        booked: true,
        appointmentId,
        date,
        time,
        calendlyStatus,
        message:
          "The calendar system couldn't finalize that slot, so it is NOT confirmed " +
          "yet. Tell the prospect: the request is in and our licensed agent will " +
          "reach out to confirm the exact time. Never present this as a done deal.",
      };
    }

    return {
      booked: true,
      appointmentId,
      date,
      time,
      calendlyStatus,
      message:
        calendlyStatus === "confirmed"
          ? `Your appointment is confirmed for ${date} at ${time}. A calendar invitation is on its way to your email.`
          : `Your appointment is confirmed for ${date} at ${time}. You'll receive a confirmation text shortly.`,
    };
  },

  add_followup_questions: async (input, { lead, sessionId }) => {
    const parsed = z.object({ condition: z.string() }).parse(input);

    let questions: AcquisitionTask[] = [];
    try {
      const resp = await coreFetch(`/api/agenda/followups`, {
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

  capture_objection: async (input, { lead, sessionId, bridge, actor, session }) => {
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

    // Retained for the handoff brief, so Ryan knows what to address.
    session.objections.push({ objectionType: parsed.objectionType, text: parsed.text });

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

  capture_dnc: async (input, { lead, sessionId, bridge, actor, session }) => {
    const parsed = z
      .object({ phoneE164: z.string(), reason: z.string().optional() })
      .parse(input);

    await bridge.publishEvent({
      eventType: "dnc_request",
      leadId: lead.leadId,
      sessionId,
      phoneE164: parsed.phoneE164,
    });

    // Revokes every selling tool on the next session.update, and forces the
    // call outcome to "dnc" so no follow-up sequence is ever scheduled.
    session.dncCaptured = true;

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

  end_call: async (input, { lead, sessionId, bridge, actor, session }) => {
    const parsed = z
      .object({ outcome: z.string().optional(), notes: z.string().optional() })
      .parse(input);

    // A DNC'd call has exactly one outcome. "callback" or "not_interested" would
    // still be a non-terminal outcome in schedule_for_outcome, and would text a
    // prospect who just asked never to be contacted again.
    let outcome = parsed.outcome;
    if (session.dncCaptured && outcome !== "dnc") {
      if (outcome) {
        logger.warn({ sessionId, claimed: outcome }, "overriding call outcome to dnc");
      }
      outcome = "dnc";
    }
    // A quote-service failure that never recovered is its own outcome — Ryan
    // needs to know a manual quote is owed. The model's explicit outcome
    // (callback, not_interested, appointment_set) still wins when it gave one.
    if (
      session.quoteFailureReason &&
      session.quote === null &&
      !session.dncCaptured &&
      (!outcome || outcome === "other")
    ) {
      outcome = "quote_failed";
    }

    // Every connected call carries its full handoff profile — this drives the
    // outcome SMS to Ryan in eastwood-core. Captured facts only.
    await bridge.publishEvent({
      eventType: "call_ended",
      leadId: lead.leadId,
      sessionId,
      outcome,
      prospectProfile: handoffProfile(session, parsed.notes),
    });
    // Suppress the hangup-path call_ended that endSession would otherwise emit
    // when the telephony stream closes a moment later.
    session.callEndedPublished = true;

    actor.send({ type: "END" });
    toolInvocations.inc({ tool_name: "end_call", outcome: "success" });

    return { ended: true, outcome };
  },

  update_lead_field: async (input, { lead, sessionId, bridge, session }) => {
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

    // Retain the captured value on the session — this is what the quote gate and
    // the handoff profile read. Without it the profile could only be invented.
    session.captured[parsed.captureKey] = parsed.value;

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
    // Tell the model exactly where it stands on quoting: the tool is gated on
    // these gaps, and the instant they close, get_quote is the next move.
    // Without this, a locked get_quote reads as "quoting is unavailable" and
    // the prospect gets told an agent will price it later — a false statement.
    const quoteReady = quoteUnlocked(lead, session);
    const missing = quoteReady ? [] : missingForQuote(lead, session);
    return {
      captured: true,
      field: parsed.field,
      remainingFields: remaining,
      nextQuestion: remaining > 0 ? (lead.acquisitionPlan?.[0]?.question ?? null) : null,
      quoteReady,
      missingForQuote: missing,
      nextStep: quoteReady
        ? session.quote
          ? undefined
          : "All quote inputs are captured — call get_quote NOW and present real numbers on this call."
        : `Real-time quoting IS available on this call; get_quote unlocks once you capture: ${missing.join("; ")}.`,
    };
  },
};
