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

const CANCER_FOLLOWUPS: AcquisitionTask[] = [
  { field: "followup_cancer_type", priority: 10, question: "What type of cancer was it?", captureKey: "cancer_type" },
  { field: "followup_treatment_end_date", priority: 11, question: "When did treatment end?", captureKey: "treatment_end_date" },
];

interface FetchStub {
  calls: string[];
  /** Force POST /api/appointments to fail, simulating core being down. */
  failAppointments: boolean;
  countCalls(suffix: string): number;
  reset(): void;
  restore(): void;
}

/** Stubs the three eastwood-core endpoints the tools call. */
function installFetchStub(): FetchStub {
  const original = globalThis.fetch;
  const stub: FetchStub = {
    calls: [],
    failAppointments: false,
    countCalls: (suffix) => stub.calls.filter((u) => u.endsWith(suffix)).length,
    reset: () => {
      stub.calls.length = 0;
      stub.failAppointments = false;
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
      return ok({
        quote_source: "estimate",
        fetched_at: new Date().toISOString(),
        // $52.30/mo on $10k face = $5.23 per $1k — inside the policy sanity band.
        best: { carrier: "Mutual of Omaha", monthly_premium: 52.3, rate_tier: "level", plan_name: null, source: "estimate" },
        all_options: [{ carrier: "Mutual of Omaha", monthly_premium: 52.3, rate_tier: "level", plan_name: null, source: "estimate" }],
        errors: [],
      });
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

/** Fire a tool call and wait for the handler to finish (a fresh session.update lands). */
async function callTool(socket: FakeOpenAiSocket, name: string, args: Record<string, unknown>): Promise<void> {
  const before = socket.sessionUpdates().length;
  socket.emitToolCall(name, args);
  await waitFor(() => socket.sessionUpdates().length > before, `${name} to be handled`);
}

async function startCall(): Promise<{
  socket: FakeOpenAiSocket;
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

  return { socket, bridge, handler, sessionId, lead };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("sales flow end-to-end", () => {
  let fetchStub: ReturnType<typeof installFetchStub>;

  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
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

  it("never gates the DNC escape hatch", async () => {
    const { socket, bridge, lead } = await startCall();

    assert.ok(socket.currentTools().includes("capture_dnc"));
    await callTool(socket, "capture_dnc", { phoneE164: lead.phoneE164, reason: "asked to be removed" });

    const dnc = bridge.find("dnc_request");
    assert.ok(dnc, "dnc_request must be published immediately");
    assert.ok(socket.currentTools().includes("end_call"), "must still be able to hang up after DNC");
  });
});
