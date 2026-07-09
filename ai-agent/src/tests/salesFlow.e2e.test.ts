/**
 * End-to-end proof that a lead can travel the full funnel:
 *   greeting → discovery → quote → appointment / transfer / end_call
 *
 * Drives the real RealtimeHandler, the real tool handlers and the real tool
 * gating. Only the OpenAI socket, Kafka and the eastwood-core HTTP API are
 * faked — everything between them is production code.
 *
 * Before the tool-gating fix this test could not get past the first assertion:
 * the machine sat in `greeting` forever, so `get_quote`, `book_appointment`,
 * `transfer_live` and `end_call` were never exposed to the model.
 */

// Must be set before any import that reads config at module scope
// (observability.ts calls loadConfig() and builds a pino logger on import;
// pino-pretty is not installed, so NODE_ENV must look like production).
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

// Imported dynamically in before() so the env vars above are in place first:
// observability.ts reads config and constructs its logger at module scope.
type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let estimatePresentation: typeof import("../tools/toolHandlers.js").estimatePresentation;

// ── Fakes ─────────────────────────────────────────────────────────────────────

/** Stands in for the OpenAI Realtime WebSocket. Records what we send it and
 *  lets the test play the role of the model calling tools. */
class FakeOpenAiSocket extends EventEmitter {
  readyState = 1; // WebSocket.OPEN
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

  /** Tool names the model is currently permitted to call. */
  currentTools(): string[] {
    const updates = this.sessionUpdates();
    const latest = updates[updates.length - 1];
    const tools = (latest?.["session"]?.["tools"] ?? []) as Array<{ name: string }>;
    return tools.map((t) => t.name);
  }

  /** The system prompt currently in force. */
  currentInstructions(): string {
    const updates = this.sessionUpdates();
    const latest = updates[updates.length - 1];
    return String(latest?.["session"]?.["instructions"] ?? "");
  }

  /** The payload the most recent tool handed back to the model. */
  lastToolResult(): Record<string, any> {
    const items = this.sent.filter((m) => m["type"] === "conversation.item.create");
    const last = items[items.length - 1];
    return JSON.parse(last!["item"]["output"]);
  }

  /** Simulate the model emitting a completed function call. */
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
  eventTypes(): string[] {
    return this.events.map((e) => e["eventType"]);
  }
  find(eventType: string): Record<string, any> | undefined {
    return this.events.find((e) => e["eventType"] === eventType);
  }
  count(eventType: string): number {
    return this.events.filter((e) => e["eventType"] === eventType).length;
  }
}

/** Stands in for the Twilio media-stream socket. Closing it is a prospect hangup. */
class FakeTelephonySocket extends EventEmitter {
  readyState = 1;
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
  /** The prospect hangs up: Twilio closes the media stream. */
  hangUp(): void {
    this.readyState = 3;
    this.emit("close");
  }
}

const CANCER_FOLLOWUPS: AcquisitionTask[] = [
  { field: "followup_cancer_type", priority: 10, question: "What type of cancer was it?", captureKey: "cancer_type" },
  { field: "followup_treatment_end_date", priority: 11, question: "When did treatment end?", captureKey: "treatment_end_date" },
];

/** How POST /api/quote should label its response. */
type QuoteMode = "estimated" | "carrier" | "unlabelled";

interface FetchStub {
  calls: string[];
  /** Force POST /api/appointments to fail, simulating core being down. */
  failAppointments: boolean;
  quoteMode: QuoteMode;
  countCalls(suffix: string): number;
  reset(): void;
  restore(): void;
}

const PREMIUM = 52.3; // $5.23 per $1k on $10k face — inside the policy sanity band

/** Mirrors the /api/quote payload eastwood-core returns for each rate source. */
function quotePayload(mode: QuoteMode): Record<string, unknown> {
  const option = { carrier: "Mutual of Omaha", monthly_premium: PREMIUM, rate_tier: "level", plan_name: null };
  if (mode === "unlabelled") {
    // A core that predates the honesty labels, or a future one that forgets them.
    return { quote_source: "estimate", best: { ...option, source: "estimate" }, all_options: [{ ...option, source: "estimate" }], errors: [] };
  }
  const labels =
    mode === "carrier"
      ? { quote_type: "carrier", rate_source: "rate_table", carrier_rate: true, bindable: false }
      : { quote_type: "estimated", rate_source: "formula_adapter", carrier_rate: false, bindable: false };

  return {
    quote_source: mode === "carrier" ? "rate_table" : "estimate",
    fetched_at: new Date().toISOString(),
    ...labels,
    best: { ...option, source: mode === "carrier" ? "rate_table" : "estimate", ...labels },
    all_options: [{ ...option, source: mode === "carrier" ? "rate_table" : "estimate", ...labels }],
    errors: [],
  };
}

/** Stubs the three eastwood-core endpoints the tools call. */
function installFetchStub(): FetchStub {
  const original = globalThis.fetch;
  const stub: FetchStub = {
    calls: [],
    failAppointments: false,
    quoteMode: "estimated",
    countCalls: (suffix) => stub.calls.filter((u) => u.endsWith(suffix)).length,
    reset: () => {
      stub.calls.length = 0;
      stub.failAppointments = false;
      stub.quoteMode = "estimated";
    },
    restore: () => {
      globalThis.fetch = original;
    },
  };
  const calls = stub.calls;

  globalThis.fetch = (async (input: any, init?: any) => {
    const url = String(input);
    calls.push(url);
    const body = init?.body ? JSON.parse(init.body) : {};
    const ok = (payload: unknown) => ({
      ok: true,
      status: 200,
      json: async () => payload,
    });

    if (url.endsWith("/api/quote")) {
      return ok(quotePayload(stub.quoteMode));
    }
    if (url.endsWith("/api/agenda/followups")) {
      return ok({ condition: body.condition, questions: CANCER_FOLLOWUPS });
    }
    if (url.endsWith("/api/appointments")) {
      if (stub.failAppointments) {
        return { ok: false, status: 503, json: async () => ({ detail: "unavailable" }) };
      }
      return ok({ appointment_id: randomUUID(), booked_at: "2026-07-10T10:00:00Z", date: body.preferred_date, time: body.preferred_time });
    }
    throw new Error(`unexpected fetch to ${url}`);
  }) as typeof fetch;

  return stub;
}

// ── Harness ───────────────────────────────────────────────────────────────────

function tier1Plan(): AcquisitionTask[] {
  return [
    { field: "age", priority: 1, question: "Could I get your age?", captureKey: "age" },
    { field: "tobacco", priority: 2, question: "Any tobacco in the past 12 months?", captureKey: "tobacco" },
    { field: "health_screening", priority: 3, question: "Any of the following conditions?", captureKey: "conditions" },
    { field: "medications", priority: 4, question: "Any prescription medications?", captureKey: "current_meds" },
    { field: "height_weight", priority: 5, question: "Height and current weight?", captureKey: "height_weight" },
  ];
}

function makeLead(): LeadContext {
  return {
    leadId: randomUUID(),
    tenantId: "default",
    phoneE164: "+13135550123",
    firstName: "Dolores",
    state: "MI",
    productLine: "final_expense",
    consentStatus: "express_written",
    acquisitionPlan: tier1Plan(),
  };
}

async function waitFor(predicate: () => boolean, label: string, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

/** Work the whole Tier-1 agenda so get_quote unlocks. */
async function completeDiscovery(socket: FakeOpenAiSocket): Promise<void> {
  for (const task of tier1Plan()) {
    await callTool(socket, "update_lead_field", { field: task.field, captureKey: task.captureKey, value: "none" });
  }
}

const QUOTE_ARGS = { productLine: "final_expense", age: 68, state: "MI", faceValue: 10_000, tobaccoUser: false };

/** Fire a tool call and wait for the handler to finish (a fresh session.update lands). */
async function callTool(socket: FakeOpenAiSocket, name: string, args: Record<string, unknown>): Promise<void> {
  const before = socket.sessionUpdates().length;
  socket.emitToolCall(name, args);
  await waitFor(() => socket.sessionUpdates().length > before, `${name} to be handled`);
}

async function startCall(): Promise<{
  socket: FakeOpenAiSocket;
  tele: FakeTelephonySocket;
  bridge: FakeBridge;
  handler: InstanceType<RealtimeHandlerCtor>;
  sessionId: string;
  lead: LeadContext;
}> {
  const bridge = new FakeBridge();
  const socket = new FakeOpenAiSocket();
  const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => socket as any);
  const sessionId = randomUUID();
  const lead = makeLead();

  await handler.createSession(sessionId, lead);
  socket.emit("open"); // triggers the first session.update
  await waitFor(() => socket.sessionUpdates().length > 0, "initial session.update");

  // Twilio connects the media stream once the prospect picks up.
  const tele = new FakeTelephonySocket();
  handler.bindTelephony(sessionId, tele as any);

  return { socket, tele, bridge, handler, sessionId, lead };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("sales flow end-to-end", () => {
  let fetchStub: ReturnType<typeof installFetchStub>;

  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
    ({ estimatePresentation } = await import("../tools/toolHandlers.js"));
    fetchStub = installFetchStub();
  });
  beforeEach(() => { fetchStub.reset(); });
  after(() => { fetchStub.restore(); });

  it("carries a clean lead from greeting through quote to a booked appointment and end_call", async () => {
    const { socket, bridge, handler, sessionId } = await startCall();

    // ── greeting: collect data, nothing else ──────────────────────────────
    let tools = socket.currentTools();
    assert.ok(tools.includes("update_lead_field"), "must be able to record answers");
    assert.ok(tools.includes("capture_dnc"), "DNC is never gated");
    assert.ok(tools.includes("end_call"), "hanging up is never gated");
    assert.ok(!tools.includes("get_quote"), "no quoting before discovery is complete");
    assert.ok(!tools.includes("book_appointment"), "no booking before a quote exists");

    // ── discovery: work the Tier-1 agenda ─────────────────────────────────
    await callTool(socket, "update_lead_field", { field: "age", captureKey: "age", value: 68 });
    await callTool(socket, "update_lead_field", { field: "tobacco", captureKey: "tobacco", value: "none" });
    await callTool(socket, "update_lead_field", { field: "health_screening", captureKey: "conditions", value: "none" });
    await callTool(socket, "update_lead_field", { field: "medications", captureKey: "current_meds", value: "none" });

    assert.ok(!socket.currentTools().includes("get_quote"), "still one Tier-1 field outstanding");

    await callTool(socket, "update_lead_field", { field: "height_weight", captureKey: "height_weight", value: "5'8\" 180 lbs" });

    // ── discovery complete: quoting unlocks ───────────────────────────────
    tools = socket.currentTools();
    assert.ok(tools.includes("get_quote"), "Tier-1 complete must unlock get_quote");
    assert.ok(tools.includes("add_followup_questions"), "late condition disclosure must still be capturable");
    assert.ok(!tools.includes("update_lead_field"), "agenda is empty, nothing left to record");
    assert.ok(!tools.includes("book_appointment"), "no booking until a quote exists");

    // ── quote ─────────────────────────────────────────────────────────────
    await callTool(socket, "get_quote", { productLine: "final_expense", age: 68, state: "MI", faceValue: 10_000, tobaccoUser: false });

    const quoteEvent = bridge.find("quote_presented");
    assert.ok(quoteEvent, "quote_presented must be published");
    assert.equal(quoteEvent["quote"].monthlyPremium, 52.3);
    assert.equal(quoteEvent["quote"].carrier, "Mutual of Omaha");

    // ── quote exists: closing tools unlock ────────────────────────────────
    tools = socket.currentTools();
    assert.ok(tools.includes("book_appointment"), "a quote must unlock booking");
    assert.ok(tools.includes("transfer_live"), "a quote must unlock the hot transfer");
    assert.ok(tools.includes("capture_consent"), "a quote must unlock consent capture");

    // ── close ─────────────────────────────────────────────────────────────
    await callTool(socket, "book_appointment", { preferredDate: "2026-07-10", preferredTime: "10:00" });

    // Exactly one appointment_booked reaches Kafka, and eastwood-core is the one
    // that publishes it. The agent writes through POST /api/appointments only.
    assert.equal(fetchStub.countCalls("/api/appointments"), 1, "core must be written to exactly once");
    assert.equal(bridge.count("appointment_booked"), 0, "agent must not publish appointment_booked — core does");

    await callTool(socket, "end_call", { outcome: "appointment_set" });
    const ended = bridge.find("call_ended");
    assert.ok(ended, "call_ended must be published — this drives follow-ups and retries in brain.py");
    assert.equal(ended["outcome"], "appointment_set");

    assert.deepEqual(bridge.eventTypes(), [
      "field_captured", "field_captured", "field_captured", "field_captured", "field_captured",
      "quote_presented",
      "call_ended",
    ]);

    // The machine never left `greeting` — and the sale still completed. That is
    // the point of the fix: tool availability is derived from lead data, not from
    // a state value nothing advances. If the machine is repaired later, this
    // assertion should be updated, not the flow above.
    assert.equal(handler.getSession(sessionId)?.state, "greeting");
  });

  it("reopens field capture when a confirmed condition pulls Tier-2 follow-ups onto the agenda", async () => {
    const { socket, bridge } = await startCall();

    await callTool(socket, "update_lead_field", { field: "age", captureKey: "age", value: 71 });
    await callTool(socket, "update_lead_field", { field: "tobacco", captureKey: "tobacco", value: "former" });
    await callTool(socket, "update_lead_field", { field: "health_screening", captureKey: "conditions", value: "cancer" });
    await callTool(socket, "update_lead_field", { field: "medications", captureKey: "current_meds", value: "tamoxifen" });
    await callTool(socket, "update_lead_field", { field: "height_weight", captureKey: "height_weight", value: "5'4\" 150 lbs" });

    assert.ok(socket.currentTools().includes("add_followup_questions"), "Tier-1 done, follow-ups available");

    // Prospect confirmed cancer — pull the condition-specific questions in.
    await callTool(socket, "add_followup_questions", { condition: "cancer" });

    const tools = socket.currentTools();
    assert.ok(tools.includes("update_lead_field"), "Tier-2 questions must be recordable");
    assert.ok(tools.includes("get_quote"), "Tier-2 follow-ups do not re-lock quoting");

    // Answer a Tier-2 question, then quote anyway.
    await callTool(socket, "update_lead_field", { field: "followup_cancer_type", captureKey: "cancer_type", value: "breast" });
    await callTool(socket, "get_quote", { productLine: "final_expense", age: 71, state: "MI", faceValue: 10_000, tobaccoUser: false });

    assert.ok(bridge.find("quote_presented"), "quote must still be reachable after Tier-2");
    assert.ok(socket.currentTools().includes("transfer_live"), "hot transfer available once quoted");
  });

  it("presents a formula quote as a non-binding estimate, never as a carrier rate", async () => {
    const { socket, bridge } = await startCall(); // quoteMode defaults to "estimated"
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);

    const result = socket.lastToolResult();

    assert.equal(result.quoteType, "estimated");
    assert.equal(result.rateSource, "formula_adapter");
    assert.equal(result.carrierRate, false);
    assert.equal(result.bindable, false);

    // No carrier is handed to the model, so no carrier can be spoken.
    assert.equal(result.carrier, null, "an estimate names no carrier");
    assert.equal(result.carrierDetermined, false);
    assert.equal(result.topOptions, undefined, "an estimate must not enumerate carriers");

    // The exact sentence, and nothing asserting finality.
    assert.equal(result.presentation, estimatePresentation(PREMIUM));
    assert.match(result.presentation, /we may be looking around \$52\.30 per month/);
    assert.match(result.presentation, /confirm the exact carrier and final rate/);
    assert.ok(!result.presentation.includes("Mutual of Omaha"), "must not name a carrier");
    for (const claim of [/guaranteed/i, /locked in/i, /\bapproved\b/i, /\bsecured\b/i, /your rate is/i, /rate is \$/i]) {
      assert.doesNotMatch(result.presentation, claim, `presentation must not claim ${claim}`);
    }

    // The event still carries the labels and the internal carrier, for the record.
    const ev = bridge.find("quote_presented")!;
    assert.equal(ev["quote"].quoteType, "estimated");
    assert.equal(ev["quote"].rateSource, "formula_adapter");
    assert.equal(ev["quote"].carrierRate, false);
    assert.equal(ev["quote"].bindable, false);
    assert.equal(ev["quote"].carrier, "Mutual of Omaha");

    // And the rail actually reaches the model.
    const instructions = socket.currentInstructions();
    assert.match(instructions, /PRESENTING A QUOTE/);
    assert.match(instructions, /Never call the number final, locked in, approved/);
  });

  it("lets a carrier-sourced quote name its carrier, but still never calls it bindable", async () => {
    const { socket, bridge } = await startCall();
    fetchStub.quoteMode = "carrier";
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);

    const result = socket.lastToolResult();

    assert.equal(result.quoteType, "carrier");
    assert.equal(result.rateSource, "rate_table");
    assert.equal(result.carrierRate, true);
    assert.equal(result.bindable, false, "nothing Eastwood quotes is bindable");
    assert.equal(result.carrier, "Mutual of Omaha");
    assert.equal(result.carrierDetermined, true);
    assert.equal(result.presentation, undefined, "the estimate script is for estimates only");
    assert.match(result.disclosure, /subject to underwriting and not bound/);

    assert.equal(bridge.find("quote_presented")!["quote"].quoteType, "carrier");
  });

  it("treats an unlabelled quote response as an estimate", async () => {
    const { socket } = await startCall();
    fetchStub.quoteMode = "unlabelled";
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);

    const result = socket.lastToolResult();

    // A core that forgets the labels must not be read as quoting a carrier rate.
    assert.equal(result.quoteType, "estimated");
    assert.equal(result.rateSource, "unknown");
    assert.equal(result.carrierRate, false);
    assert.equal(result.bindable, false);
    assert.equal(result.carrier, null);
    assert.equal(result.presentation, estimatePresentation(PREMIUM));
  });

  it("does not claim a booking when eastwood-core rejects the write", async () => {
    const { socket, bridge } = await startCall();
    fetchStub.failAppointments = true;

    for (const task of tier1Plan()) {
      await callTool(socket, "update_lead_field", { field: task.field, captureKey: task.captureKey, value: "none" });
    }
    await callTool(socket, "get_quote", { productLine: "final_expense", age: 68, state: "MI", faceValue: 10_000, tobaccoUser: false });
    await callTool(socket, "book_appointment", { preferredDate: "2026-07-10", preferredTime: "10:00" });

    assert.equal(bridge.count("appointment_booked"), 0, "a failed write must publish nothing");

    // The model must learn the booking failed, not be told it succeeded.
    const results = socket.sent.filter((m) => m["type"] === "conversation.item.create");
    const last = JSON.parse(results[results.length - 1]!["item"]["output"]);
    assert.equal(last.booked, false);
    assert.ok(last.error, "must surface an error the agent can speak");
  });

  it("emits call_ended when the prospect hangs up without the model calling end_call", async () => {
    const { socket, tele, bridge, sessionId, handler } = await startCall();

    for (const task of tier1Plan()) {
      await callTool(socket, "update_lead_field", { field: task.field, captureKey: task.captureKey, value: "none" });
    }
    await callTool(socket, "get_quote", { productLine: "final_expense", age: 68, state: "MI", faceValue: 10_000, tobaccoUser: false });

    assert.equal(bridge.count("call_ended"), 0, "no outcome yet — the call is live");

    tele.hangUp(); // Twilio closes the media stream
    await waitFor(() => bridge.count("call_ended") === 1, "call_ended after hangup");

    const ended = bridge.find("call_ended")!;
    // They heard a quote and left without booking: brain.py maps this to the
    // quoted_not_closed follow-up sequence.
    assert.equal(ended["outcome"], "quoted_not_closed");
    assert.equal(ended["sessionId"], sessionId);
    assert.equal(typeof ended["durationSeconds"], "number");
    assert.equal(handler.getSession(sessionId), null, "session is torn down");
  });

  it("emits exactly one call_ended when end_call is followed by the stream closing", async () => {
    const { socket, tele, bridge } = await startCall();

    await callTool(socket, "end_call", { outcome: "not_interested" });
    assert.equal(bridge.count("call_ended"), 1);

    tele.hangUp(); // the stream always closes right after
    await new Promise((r) => setTimeout(r, 30));

    assert.equal(bridge.count("call_ended"), 1, "endSession must not emit a duplicate outcome");
    assert.equal(bridge.find("call_ended")!["outcome"], "not_interested", "the model's outcome wins");
  });

  it("reports a hangup before any quote as a plain hangup", async () => {
    const { tele, bridge } = await startCall();

    tele.hangUp();
    await waitFor(() => bridge.count("call_ended") === 1, "call_ended after early hangup");

    assert.equal(bridge.find("call_ended")!["outcome"], "hangup");
  });

  it("never gates the DNC escape hatch", async () => {
    const { socket, bridge, lead } = await startCall();

    assert.ok(socket.currentTools().includes("capture_dnc"));
    await callTool(socket, "capture_dnc", { phoneE164: lead.phoneE164, reason: "asked to be removed" });

    const dnc = bridge.find("dnc_request");
    assert.ok(dnc, "dnc_request must be published immediately");
    assert.ok(socket.currentTools().includes("end_call"), "must still be able to hang up after DNC");
  });

  it("revokes every selling tool the moment a DNC is captured", async () => {
    const { socket, lead } = await startCall();

    // Get as far as possible first: quoted, ready to book.
    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);
    assert.ok(socket.currentTools().includes("book_appointment"), "precondition: closing tools are live");

    await callTool(socket, "capture_dnc", { phoneE164: lead.phoneE164, reason: "take me off your list" });

    // Only one thing left to do.
    assert.deepEqual(socket.currentTools(), ["end_call"]);
    for (const tool of ["get_quote", "book_appointment", "transfer_live", "transfer_to_human", "update_lead_field", "capture_consent", "capture_objection"]) {
      assert.ok(!socket.currentTools().includes(tool), `${tool} must be revoked after a DNC`);
    }
  });

  it("forces the call outcome to dnc even when the model reports otherwise", async () => {
    const { socket, bridge, lead } = await startCall();

    await callTool(socket, "capture_dnc", { phoneE164: lead.phoneE164 });
    await callTool(socket, "end_call", { outcome: "callback" });

    // "callback" is non-terminal in schedule_for_outcome — it would text them.
    assert.equal(bridge.find("call_ended")!["outcome"], "dnc");
    assert.equal(socket.lastToolResult().outcome, "dnc");
  });

  it("reports a hangup after a DNC as dnc, not as an unclosed quote", async () => {
    const { socket, tele, bridge, lead } = await startCall();

    await completeDiscovery(socket);
    await callTool(socket, "get_quote", QUOTE_ARGS);
    await callTool(socket, "capture_dnc", { phoneE164: lead.phoneE164 });

    tele.hangUp(); // they hang up rather than letting her wrap up
    await waitFor(() => bridge.count("call_ended") === 1, "call_ended after DNC hangup");

    // Without this, brain.py schedules the quoted_not_closed sequence: three SMS
    // over a week to someone who just asked never to be contacted again.
    assert.equal(bridge.find("call_ended")!["outcome"], "dnc");
  });
});
