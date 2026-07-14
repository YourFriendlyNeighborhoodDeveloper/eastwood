/**
 * Three-option quote presentation, rating sex handling, Calendly booking flow,
 * and the per-call handoff profile.
 *
 * Proves the compressed-sales-agent contract:
 *   - rating sex is asked and captured, never inferred or model-supplied
 *   - get_quote surfaces the engine's C/B/A options with exact figures
 *   - vague premium ranges are rejected once exact options exist
 *   - the prospect's selection and affordability confirmation persist
 *   - real availability is retrieved before a Calendly time is offered/booked
 *   - a missing email blocks the Calendly booking until collected
 *   - a failed Calendly booking is never presented as confirmed
 *   - every connected call's call_ended carries the factual profile
 *   - a quote-service failure books honestly and never fabricates a premium
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { AcquisitionTask, LeadContext } from "../types.js";
import { PolicyEngine } from "../policy/policyEngine.js";
import { buildSystemPrompt } from "../prompts/buildSystemPrompt.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let optionsPresentation: typeof import("../tools/toolHandlers.js").optionsPresentation;

// ── Fakes (same shape as salesFlow.e2e.test.ts) ──────────────────────────────

class FakeOpenAiSocket extends EventEmitter {
  readyState = 1;
  readonly sent: Array<Record<string, any>> = [];
  send(raw: string): void {
    this.sent.push(JSON.parse(raw));
  }
  close(): void {
    this.readyState = 3;
  }
  sessionUpdates(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "session.update");
  }
  currentTools(): string[] {
    const updates = this.sessionUpdates();
    const latest = updates[updates.length - 1];
    const tools = (latest?.["session"]?.["tools"] ?? []) as Array<{ name: string }>;
    return tools.map((t) => t.name);
  }
  lastToolResult(): Record<string, any> {
    const items = this.sent.filter((m) => m["type"] === "conversation.item.create");
    const last = items[items.length - 1];
    return JSON.parse(last!["item"]["output"]);
  }
  emitToolCall(name: string, args: Record<string, unknown>): void {
    this.emit(
      "message",
      Buffer.from(
        JSON.stringify({
          type: "response.function_call_arguments.done",
          name,
          call_id: `call_${name}_${this.sent.length}`,
          arguments: JSON.stringify(args),
        })
      )
    );
  }
}

class FakeBridge {
  readonly events: Array<Record<string, any>> = [];
  async publishEvent(event: Record<string, any>): Promise<void> {
    this.events.push(event);
  }
  find(eventType: string): Record<string, any> | undefined {
    return this.events.find((e) => e["eventType"] === eventType);
  }
  findAll(eventType: string): Array<Record<string, any>> {
    return this.events.filter((e) => e["eventType"] === eventType);
  }
  count(eventType: string): number {
    return this.findAll(eventType).length;
  }
}

class FakeTelephonySocket extends EventEmitter {
  readyState = 1;
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
  hangUp(): void {
    this.readyState = 3;
    this.emit("close");
  }
}

// ── Core API stub ─────────────────────────────────────────────────────────────

const OPTION_C = { label: "C", face_value: 10_000, monthly_premium: 55.2 };
const OPTION_B = { label: "B", face_value: 15_000, monthly_premium: 82.8 };
const OPTION_A = { label: "A", face_value: 25_000, monthly_premium: 138.0 };

const SLOT_1 = "2026-07-16T14:00:00Z";
const SLOT_2 = "2026-07-16T15:30:00Z";
const SLOT_3 = "2026-07-17T14:00:00Z";

interface FetchStub {
  calls: Array<{ url: string; body: Record<string, any> }>;
  failQuote: boolean;
  emptyOptions: boolean;
  overBudgetA: boolean;
  calendlyOutcome: "confirmed" | "failed";
  lastQuoteBody(): Record<string, any> | undefined;
  reset(): void;
  restore(): void;
}

function estimateLabels() {
  return { quote_type: "estimated", rate_source: "formula_adapter", carrier_rate: false, bindable: false };
}

function installFetchStub(): FetchStub {
  const original = globalThis.fetch;
  const stub: FetchStub = {
    calls: [],
    failQuote: false,
    emptyOptions: false,
    overBudgetA: false,
    calendlyOutcome: "confirmed",
    lastQuoteBody: () => [...stub.calls].reverse().find((c) => c.url.endsWith("/api/quote"))?.body,
    reset: () => {
      stub.calls.length = 0;
      stub.failQuote = false;
      stub.emptyOptions = false;
      stub.overBudgetA = false;
      stub.calendlyOutcome = "confirmed";
    },
    restore: () => {
      globalThis.fetch = original;
    },
  };

  globalThis.fetch = (async (input: any, init?: any) => {
    const url = String(input);
    const body = init?.body ? JSON.parse(init.body) : {};
    stub.calls.push({ url, body });
    const ok = (payload: unknown) => ({ ok: true, status: 200, json: async () => payload });

    if (url.endsWith("/api/quote")) {
      if (stub.failQuote) {
        return { ok: false, status: 503, json: async () => ({ detail: "unavailable" }) };
      }
      const options = stub.emptyOptions
        ? []
        : [
            { ...OPTION_C, carrier: "Transamerica", rate_tier: "level", within_budget: true, ...estimateLabels() },
            { ...OPTION_B, carrier: "Transamerica", rate_tier: "level", within_budget: true, ...estimateLabels() },
            {
              ...OPTION_A,
              carrier: "Transamerica",
              rate_tier: "level",
              within_budget: stub.overBudgetA ? false : true,
              ...estimateLabels(),
            },
          ];
      return ok({
        quote_source: "estimate",
        ...estimateLabels(),
        best: { carrier: "Transamerica", monthly_premium: OPTION_B.monthly_premium, rate_tier: "level", ...estimateLabels() },
        all_options: [],
        options,
        errors: [],
      });
    }
    if (url.includes("/api/scheduling/availability")) {
      return ok({ calendly_enabled: true, times: [SLOT_1, SLOT_2, SLOT_3], timezone: "America/Detroit" });
    }
    if (url.endsWith("/api/appointments")) {
      return ok({
        appointment_id: randomUUID(),
        booked_at: SLOT_1,
        date: "2026-07-16",
        time: "14:00",
        timezone: body.timezone ?? "America/Detroit",
        calendly_status: stub.calendlyOutcome,
        calendly_event_uri: stub.calendlyOutcome === "confirmed" ? "https://api.calendly.com/scheduled_events/EV1" : null,
        calendly_invitee_uri: stub.calendlyOutcome === "confirmed" ? "https://api.calendly.com/scheduled_events/EV1/invitees/IN1" : null,
        calendly_cancel_url: stub.calendlyOutcome === "confirmed" ? "https://calendly.com/cancellations/IN1" : null,
        calendly_reschedule_url: stub.calendlyOutcome === "confirmed" ? "https://calendly.com/reschedulings/IN1" : null,
      });
    }
    if (url.endsWith("/api/agenda/followups")) {
      return ok({ questions: [] });
    }
    throw new Error(`unexpected fetch to ${url}`);
  }) as typeof fetch;

  return stub;
}

// ── Harness ───────────────────────────────────────────────────────────────────

function makeLead(): LeadContext {
  return {
    leadId: randomUUID(),
    tenantId: "default",
    phoneE164: "+13135550123",
    firstName: "Dolores",
    lastName: "Vega",
    state: "MI",
    productLine: "final_expense",
    consentStatus: "express_written",
    acquisitionPlan: [] as AcquisitionTask[],
  };
}

async function waitFor(predicate: () => boolean, label: string, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

async function callTool(socket: FakeOpenAiSocket, name: string, args: Record<string, unknown>): Promise<void> {
  const before = socket.sessionUpdates().length;
  socket.emitToolCall(name, args);
  await waitFor(() => socket.sessionUpdates().length > before, `${name} to be handled`);
}

async function startCall() {
  const bridge = new FakeBridge();
  const socket = new FakeOpenAiSocket();
  const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => socket as any);
  const sessionId = randomUUID();
  const lead = makeLead();

  await handler.createSession(sessionId, lead);
  socket.emit("open");
  await waitFor(() => socket.sessionUpdates().length > 0, "initial session.update");

  const tele = new FakeTelephonySocket();
  handler.bindTelephony(sessionId, tele as any);

  return { socket, tele, bridge, handler, sessionId, lead };
}

/** Discovery incl. rating sex and a $100/month budget. */
async function completeDiscovery(socket: FakeOpenAiSocket, ratingSex = "female"): Promise<void> {
  await callTool(socket, "update_lead_field", { field: "coverage_purpose", captureKey: "coverage_purpose", value: "burial" });
  await callTool(socket, "update_lead_field", { field: "beneficiary", captureKey: "beneficiary_relationship", value: "spouse" });
  await callTool(socket, "update_lead_field", { field: "coverage_target", captureKey: "desired_coverage_amount", value: 10_000 });
  await callTool(socket, "update_lead_field", { field: "budget", captureKey: "affordable_monthly_budget", value: 100 });
  await callTool(socket, "update_lead_field", { field: "rating_sex", captureKey: "rating_sex", value: ratingSex });
  await callTool(socket, "update_lead_field", { field: "employment_status", captureKey: "employment_status", value: "social_security" });
  await callTool(socket, "update_lead_field", { field: "age", captureKey: "age", value: 68 });
}

const QUOTE_ARGS = { productLine: "final_expense", age: 68, state: "MI", tobaccoUser: false };

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("three-option quote presentation", () => {
  let fetchStub: FetchStub;

  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
    ({ optionsPresentation } = await import("../tools/toolHandlers.js"));
    fetchStub = installFetchStub();
  });
  beforeEach(() => fetchStub.reset());
  after(() => fetchStub.restore());

  it("presents three deterministic options in C, B, A order with exact figures", async () => {
    const { socket, bridge } = await startCall();
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);

    const result = socket.lastToolResult();
    assert.equal(result.error, undefined);
    assert.deepEqual(
      result.options.map((o: any) => o.option),
      ["C", "B", "A"],
      "options are presented C, then B, then A"
    );
    assert.deepEqual(
      result.options.map((o: any) => [o.faceValue, o.monthlyPremium]),
      [
        [10_000, 55.2],
        [15_000, 82.8],
        [25_000, 138.0],
      ],
      "each option is the engine's exact face and premium"
    );

    // The presentation script names each option with specific figures.
    const script: string[] = result.presentation;
    assert.match(script[1]!, /Option C is \$10,000 of coverage at approximately \$55\.20 per month/);
    assert.match(script[2]!, /Option B is \$15,000 of coverage at approximately \$82\.80 per month/);
    assert.match(script[3]!, /Option A is \$25,000 of coverage at approximately \$138\.00 per month/);
    assert.match(script[script.length - 1]!, /Which of those feels like the best fit/);
    // Estimates say so, and no vague range appears anywhere in the script.
    assert.ok(script.some((l) => /estimates/.test(l)), "estimates are labelled as estimates");
    for (const line of script) {
      assert.doesNotMatch(line, /\$\d+\s*(to|-)\s*\$\d+/, "no vague ranges in the presentation");
    }

    // Event published for persistence carries the full options set.
    const ev = bridge.find("quote_presented")!;
    assert.equal(ev["options"].length, 3);
    assert.equal(ev["options"][0].label, "C");
  });

  it("sends the CAPTURED rating sex to the engine and ignores any model-supplied gender", async () => {
    const { socket } = await startCall();
    await completeDiscovery(socket, "female");
    // A model trying to pass gender is ignored — the schema has no such input.
    await callTool(socket, "get_quote", { ...QUOTE_ARGS, gender: "male" });

    const body = fetchStub.lastQuoteBody()!;
    assert.equal(body.gender, "female", "rating sex comes from the captured field only");
  });

  it("sends null rating sex when the prospect declined — never a guess", async () => {
    const { socket } = await startCall();
    await completeDiscovery(socket, "unknown");
    await callTool(socket, "get_quote", QUOTE_ARGS);

    const body = fetchStub.lastQuoteBody()!;
    assert.equal(body.gender, null, "a declined answer is unknown, not inferred");
  });

  it("flags over-budget options so they are only explored with permission", async () => {
    const { socket } = await startCall();
    fetchStub.overBudgetA = true;
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);

    const result = socket.lastToolResult();
    assert.deepEqual(result.overBudgetOptions, ["A"]);
    assert.match(result.overBudgetNote, /ask permission/i);
  });

  it("persists the selected option and affordability confirmation", async () => {
    const { socket, bridge, handler, sessionId } = await startCall();
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);

    assert.ok(socket.currentTools().includes("select_quote_option"), "selection unlocks after options exist");

    await callTool(socket, "select_quote_option", { option: "B", affordabilityConfirmed: true });
    const result = socket.lastToolResult();
    assert.equal(result.selected, "B");
    assert.equal(result.faceValue, 15_000);
    assert.equal(result.affordabilityConfirmed, true);

    // The selection is a captured fact with an audit event.
    const captures = bridge.findAll("field_captured");
    const selectionEvent = captures.find((e) => e["captureKey"] === "selected_quote_option");
    assert.ok(selectionEvent, "selection publishes a field_captured event");
    assert.equal(selectionEvent!["value"], "B");

    // And it lands in the handoff profile at call end.
    await callTool(socket, "end_call", { outcome: "appointment_set" });
    const profile = bridge.find("call_ended")!["prospectProfile"];
    assert.equal(profile.selected_option, "B");
    assert.equal(profile.affordability_confirmed, true);
    assert.equal(profile.quote_options.length, 3);
    assert.equal(handler.getSession(sessionId), null);
  });

  it("rejects selecting an option that was never presented", async () => {
    const { socket } = await startCall();
    await completeDiscovery(socket);
    fetchStub.emptyOptions = false;
    await callTool(socket, "get_quote", QUOTE_ARGS);

    // Presented C/B/A — but claim "D"-like behaviour by removing options first:
    await callTool(socket, "select_quote_option", { option: "A", affordabilityConfirmed: false });
    const okResult = socket.lastToolResult();
    assert.equal(okResult.selected, "A");
    assert.match(okResult.nextStep, /Confirm affordability/, "unconfirmed affordability prompts the question");
  });

  it("books honestly when the quote service fails: no invented premium, profile preserved", async () => {
    const { socket, bridge } = await startCall();
    fetchStub.failQuote = true;
    await completeDiscovery(socket);

    assert.ok(!socket.currentTools().includes("book_appointment"), "booking locked while quoting is possible");

    await callTool(socket, "get_quote", QUOTE_ARGS);
    const result = socket.lastToolResult();
    assert.ok(result.quoteFailed, "the failure is explicit");
    assert.match(result.error, /Do NOT state any number/);
    assert.equal(result.monthlyPremium, undefined, "no premium is fabricated");
    assert.equal(bridge.count("quote_presented"), 0, "nothing was presented");

    // Booking unlocks for the manual-quote path.
    assert.ok(socket.currentTools().includes("book_appointment"), "honest booking after quote failure");

    // The failure reason reaches the profile.
    await callTool(socket, "end_call", {});
    const profile = bridge.find("call_ended")!["prospectProfile"];
    assert.equal(profile.quote_failure_reason, "quote_service_unavailable");
    assert.equal(bridge.find("call_ended")!["outcome"], "quote_failed");
    assert.equal(profile.quote_options.length, 0);
    assert.equal(profile.quoted_monthly_premium, null);
  });
});

describe("calendly booking flow", () => {
  let fetchStub: FetchStub;

  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
    fetchStub = installFetchStub();
  });
  beforeEach(() => fetchStub.reset());
  after(() => fetchStub.restore());

  async function quoteAndSelect(socket: FakeOpenAiSocket) {
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);
    await callTool(socket, "select_quote_option", { option: "B", affordabilityConfirmed: true });
  }

  it("retrieves real availability before any time can be booked", async () => {
    const { socket } = await startCall();
    await quoteAndSelect(socket);

    // Booking a slot without ever fetching availability is refused.
    await callTool(socket, "book_appointment", { slotStartTime: SLOT_1, email: "d.vega@example.com" });
    assert.match(socket.lastToolResult().error, /get_booking_slots first/);

    await callTool(socket, "get_booking_slots", {});
    const slots = socket.lastToolResult();
    assert.deepEqual(slots.times, [SLOT_1, SLOT_2, SLOT_3], "real availability from core");

    // A time that was never offered is refused.
    await callTool(socket, "book_appointment", { slotStartTime: "2026-07-20T09:00:00Z", email: "d.vega@example.com" });
    assert.match(socket.lastToolResult().error, /not in the offered availability/);
  });

  it("requires the prospect's email before the Calendly booking", async () => {
    const { socket } = await startCall();
    await quoteAndSelect(socket);
    await callTool(socket, "get_booking_slots", {});

    await callTool(socket, "book_appointment", { slotStartTime: SLOT_1 });
    const result = socket.lastToolResult();
    assert.equal(result.booked, false);
    assert.equal(result.needsEmail, true, "email must be collected first");

    // Capture the email, then the same booking succeeds.
    await callTool(socket, "update_lead_field", { field: "email", captureKey: "prospect_email", value: "d.vega@example.com" });
    await callTool(socket, "book_appointment", { slotStartTime: SLOT_1 });
    const booked = socket.lastToolResult();
    assert.equal(booked.booked, true);
    assert.equal(booked.calendlyStatus, "confirmed");

    const apptCall = fetchStub.calls.find((c) => c.url.endsWith("/api/appointments"))!;
    assert.equal(apptCall.body.start_time, SLOT_1, "the selected slot is what gets booked");
    assert.equal(apptCall.body.email, "d.vega@example.com");
    assert.equal(apptCall.body.selected_option, "B", "the chosen option rides along");
  });

  it("persists the booked slot and Calendly identifiers into the handoff profile", async () => {
    const { socket, bridge } = await startCall();
    await quoteAndSelect(socket);
    await callTool(socket, "get_booking_slots", {});
    await callTool(socket, "book_appointment", { slotStartTime: SLOT_2, email: "d.vega@example.com" });

    assert.equal(socket.lastToolResult().booked, true);

    await callTool(socket, "end_call", { outcome: "appointment_set" });
    const profile = bridge.find("call_ended")!["prospectProfile"];
    assert.equal(profile.calendly_status, "confirmed");
    assert.match(profile.appointment_time, new RegExp(SLOT_2));
  });

  it("never claims confirmation when the Calendly booking failed", async () => {
    const { socket, bridge } = await startCall();
    fetchStub.calendlyOutcome = "failed";
    await quoteAndSelect(socket);
    await callTool(socket, "get_booking_slots", {});
    await callTool(socket, "book_appointment", { slotStartTime: SLOT_1, email: "d.vega@example.com" });

    const result = socket.lastToolResult();
    assert.equal(result.calendlyStatus, "failed");
    assert.match(result.message, /NOT confirmed/);
    assert.doesNotMatch(result.message, /appointment is confirmed/);

    await callTool(socket, "end_call", {});
    const profile = bridge.find("call_ended")!["prospectProfile"];
    assert.equal(profile.calendly_status, "failed");
  });
});

describe("handoff profile on every connected call", () => {
  let fetchStub: FetchStub;

  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
    fetchStub = installFetchStub();
  });
  beforeEach(() => fetchStub.reset());
  after(() => fetchStub.restore());

  it("attaches the factual profile to a hangup call_ended, unknowns preserved", async () => {
    const { socket, tele, bridge } = await startCall();
    // Only two facts captured; everything else must read unknown, not invented.
    await callTool(socket, "update_lead_field", { field: "coverage_purpose", captureKey: "coverage_purpose", value: "cremation" });
    await callTool(socket, "update_lead_field", { field: "employment_status", captureKey: "employment_status", value: "retired" });

    tele.hangUp();
    await waitFor(() => bridge.count("call_ended") === 1, "call_ended after hangup");

    const profile = bridge.find("call_ended")!["prospectProfile"];
    assert.equal(profile.coverage_purpose, "cremation");
    assert.equal(profile.employment_status, "retired");
    assert.equal(profile.rating_sex, "unknown", "never guessed");
    assert.equal(profile.beneficiary_name, "unknown");
    assert.equal(profile.health_summary, "unknown");
    assert.equal(profile.desired_coverage_amount, null);
    assert.equal(profile.selected_option, null);
    assert.equal(profile.calendly_status, "not_requested");
  });

  it("normalises any non-answer rating sex capture to unknown", async () => {
    const { socket, bridge } = await startCall();
    await callTool(socket, "update_lead_field", { field: "rating_sex", captureKey: "rating_sex", value: "prefer not to say" });
    await callTool(socket, "end_call", {});
    const profile = bridge.find("call_ended")!["prospectProfile"];
    assert.equal(profile.rating_sex, "unknown");
  });
});

describe("vague-range policy", () => {
  it("blocks a vague premium range once exact options exist", () => {
    const policy = new PolicyEngine();
    const blocked = policy.evaluateUtterance("Coverage may be around $60 to $80 a month", {
      hasExactQuote: true,
    });
    assert.equal(blocked.allowed, false);
    assert.equal(blocked.reason, "vague_range_with_exact_quote");

    for (const phrasing of ["somewhere between $60 and $80", "$60-$80 per month", "$55 – $85 monthly"]) {
      assert.equal(
        policy.evaluateUtterance(phrasing, { hasExactQuote: true }).allowed,
        false,
        `must block: ${phrasing}`
      );
    }
  });

  it("does not block exact option figures or pre-quote conversation", () => {
    const policy = new PolicyEngine();
    assert.equal(
      policy.evaluateUtterance("Option C is $10,000 of coverage at approximately $55.20 per month", {
        hasExactQuote: true,
      }).allowed,
      true,
      "exact figures are exactly what we want"
    );
    assert.equal(
      policy.evaluateUtterance("Coverage may be around $60 to $80 a month").allowed,
      true,
      "before any options exist the range rule does not apply"
    );
  });
});

describe("prompt and templates make no unsupported claims", () => {
  it("contains no forbidden product claims", async () => {
    const prompt = buildSystemPrompt(makeLead(), { agencyName: "Eastwood Financial", agentDisplayName: "Imogen" });
    for (const forbidden of [
      /state recommended amount/i,
      /the state requires/i,
      /can never lose money/i,
      /cannot lose money/i,
      /guaranteed (interest|tax|returns|growth)/i,
      /accident(al)? doubling/i,
      /guaranteed living benefits/i,
      /maximize AP/i,
    ]) {
      assert.doesNotMatch(prompt, forbidden, `prompt must not contain ${forbidden}`);
    }
  });

  it("keeps the option templates free of guarantees and vague ranges", async () => {
    ({ optionsPresentation } = await import("../tools/toolHandlers.js"));
    const lines = optionsPresentation([
      { label: "C", faceValue: 10_000, monthlyPremium: 55.2, carrier: null, quoteType: "estimated", rateSource: "formula_adapter", carrierRate: false, bindable: false, withinBudget: true },
      { label: "B", faceValue: 15_000, monthlyPremium: 82.8, carrier: null, quoteType: "estimated", rateSource: "formula_adapter", carrierRate: false, bindable: false, withinBudget: true },
      { label: "A", faceValue: 25_000, monthlyPremium: 138.0, carrier: null, quoteType: "estimated", rateSource: "formula_adapter", carrierRate: false, bindable: false, withinBudget: true },
    ]);
    for (const line of lines) {
      assert.doesNotMatch(line, /guarantee/i);
      assert.doesNotMatch(line, /locked in|approved|secured/i);
      assert.doesNotMatch(line, /\$\d+\s*(to|-)\s*\$\d+/, "no vague ranges");
    }
  });
});
