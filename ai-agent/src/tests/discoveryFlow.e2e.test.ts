/**
 * The discovery flow as a state machine, driven through the real handler.
 *
 *   ASK_BENEFICIARY → wait → transcript
 *     valid?  yes → store → ASK_COVERAGE
 *             no  → one clarification → wait
 *                     still bad → abandon the step → ASK_COVERAGE
 *
 * Before this existed, "which question are we on" was the model's to remember,
 * read out of a prose agenda in the system prompt. `salesMachine` looks like it
 * owns this and does not — nothing sends ADVANCE, so it reports `greeting` for
 * the whole call. A traced 208-second call never established one slot.
 *
 * These assert the two branches and, most importantly, that an abandoned step
 * is never returned to — the flow is derived from captured slots, and an
 * abandoned slot is exactly a slot that is not captured, so without the skip
 * set the machine would hand back the question it just gave up on.
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["WATCHDOG_STALL_MS"] = "30000";
process.env["TURN_GATE_MAX_WAIT_MS"] = "200";
process.env["TURN_GATE_MAX_ATTEMPTS"] = "2";
process.env["TURN_GATE_ESCALATE_AFTER"] = "3";
process.env["TRANSCRIPT_CONFIDENCE_MIN"] = "0.6";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext, SessionState } from "../types.js";

type Flow = typeof import("../discovery/discoveryFlow.js");
let currentStepId: Flow["currentStepId"];
let discoveryStateBlock: Flow["discoveryStateBlock"];
let DISCOVERY_FLOW: Flow["DISCOVERY_FLOW"];

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;

const CLEAR = Array(8).fill({ logprob: -0.02 });

class Sock extends EventEmitter {
  readyState = 1;
  readonly sent: Array<Record<string, any>> = [];
  send(raw: string): void {
    this.sent.push(JSON.parse(raw));
  }
  close(): void {
    this.readyState = 3;
  }
  emitRaw(p: Record<string, unknown>): void {
    this.emit("message", Buffer.from(JSON.stringify(p)));
  }
  responses(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "response.create");
  }
  instructions(): string[] {
    return this.responses().map((m) => String(m["response"]?.["instructions"] ?? ""));
  }
  /** The state block on the most recent session.update. */
  lastStateBlock(): string {
    const u = this.sent.filter((m) => m["type"] === "session.update").at(-1);
    const instr = String(u?.["session"]?.["instructions"] ?? "");
    const i = instr.indexOf("WHERE THIS CALL ACTUALLY IS");
    return i === -1 ? "" : instr.slice(i);
  }
  toolResults(): Array<Record<string, any>> {
    return this.sent
      .filter(
        (m) =>
          m["type"] === "conversation.item.create" &&
          m["item"]?.["type"] === "function_call_output"
      )
      .map((m) => JSON.parse(m["item"]["output"]));
  }
}
class Bridge {
  async publishEvent(): Promise<void> {}
}
class Tele extends EventEmitter {
  readyState = 1;
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
}

function makeLead(): LeadContext {
  return {
    leadId: randomUUID(),
    tenantId: "default",
    phoneE164: "+13135550123",
    firstName: "Dolores",
    age: 62,
    state: "MI",
    productLine: "final_expense",
    consentStatus: "express_written",
    acquisitionPlan: [],
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
async function waitFor(p: () => boolean, label: string, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!p()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await sleep(5);
  }
}

interface Call {
  socket: Sock;
  handler: InstanceType<RealtimeHandlerCtor>;
  sessionId: string;
  session(): SessionState & { skippedSteps: Set<any> };
  step(): string;
  asks(q: string): void;
  answers(text: string): Promise<void>;
  records(captureKey: string, value: unknown, verbatim: string): Promise<Record<string, any>>;
  end(): void;
}

async function startCall(): Promise<Call> {
  const socket = new Sock();
  const handler = new RealtimeHandler(new Bridge() as unknown as KafkaBridge, () => socket as any);
  const sessionId = randomUUID();
  const lead = makeLead();
  await handler.createSession(sessionId, lead);
  socket.emit("open");
  socket.emitRaw({ type: "session.created" });
  const tele = new Tele();
  handler.bindTelephony(sessionId, tele as any);
  tele.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid: "MZ1" })));
  socket.emitRaw({ type: "response.done" });

  const session = () => (handler as any).sessions.get(sessionId);

  return {
    socket,
    handler,
    sessionId,
    session,
    step: () => currentStepId(session(), session().lead, session().skippedSteps),
    asks(q: string) {
      socket.emitRaw({ type: "response.created" });
      for (const w of q.split(/(?<=\s)/)) {
        socket.emitRaw({ type: "response.audio_transcript.delta", delta: w });
      }
      socket.emitRaw({ type: "response.done" });
    },
    async answers(text: string) {
      const before = socket.responses().length;
      socket.emitRaw({ type: "input_audio_buffer.speech_started" });
      await sleep(5);
      socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
      if (text !== "") {
        await sleep(10);
        socket.emitRaw({
          type: "conversation.item.input_audio_transcription.completed",
          transcript: text,
          logprobs: CLEAR,
        });
      }
      await waitFor(() => socket.responses().length > before, `a ruling on "${text}"`);
    },
    async records(captureKey: string, value: unknown, verbatim: string) {
      const n = socket.toolResults().length;
      socket.emitRaw({
        type: "response.function_call_arguments.done",
        name: "update_lead_field",
        call_id: `c_${Math.random().toString(36).slice(2, 8)}`,
        arguments: JSON.stringify({ field: captureKey, captureKey, value, heardVerbatim: verbatim }),
      });
      await waitFor(() => socket.toolResults().length > n, `tool result for ${captureKey}`);
      socket.emitRaw({ type: "response.done" });
      return socket.toolResults().at(-1)!;
    },
    end() {
      void handler.endSession(sessionId, "test");
    },
  };
}

before(async () => {
  ({ currentStepId, discoveryStateBlock, DISCOVERY_FLOW } = await import(
    "../discovery/discoveryFlow.js"
  ));
  ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
});

describe("the discovery flow advances on a valid answer", () => {
  it("starts on the first unanswered step", async () => {
    const call = await startCall();
    assert.equal(call.step(), "ASK_PURPOSE");
    call.end();
  });

  it("moves to the next step only when the slot is CONFIRMED", async () => {
    const call = await startCall();
    call.asks("Who are you mainly trying to make sure is protected?");
    await call.answers("My daughter, Marie.");

    assert.equal(call.step(), "ASK_PURPOSE", "nothing recorded yet, so nothing advances");

    const r = await call.records("beneficiary_name", "Marie", "My daughter, Marie.");
    assert.equal(r["slotState"], "CONFIRMED");
    assert.equal(
      call.step(),
      "ASK_PURPOSE",
      "beneficiary is recorded, but purpose is still the first open step"
    );
    call.end();
  });

  it("does not advance on a slot that is only HEARD", async () => {
    // The whole reason `captured` is the authority: a value being read back is
    // not an answer yet, and advancing past it would strand it forever.
    const call = await startCall();
    call.asks("For rating purposes, are you male or female?");
    await call.answers("Female.");
    // A quote that names no alternative cannot confirm a choice — it is held.
    const r = await call.records("rating_sex", "female", "I don't want my daughter stuck.");
    assert.notEqual(r["slotState"], "CONFIRMED");
    assert.ok(!DISCOVERY_FLOW.some((s) => s.id === call.step() && s.id === "ASK_TOBACCO"));
    call.end();
  });
});

describe("the discovery flow abandons a step it cannot answer", () => {
  /** One question asked and answered with nothing usable. */
  async function badTurn(call: Call, question: string): Promise<void> {
    call.asks(question);
    await call.answers("[background noise]");
  }

  it("clarifies once, stays on the step, then abandons it and moves on", async () => {
    const call = await startCall();
    const q = "What's got you looking into this?";
    assert.equal(call.step(), "ASK_PURPOSE");

    await badTurn(call, q);
    assert.match(
      call.socket.instructions().at(-1)!,
      /YOU DID NOT GET AN ANSWER/i,
      "first failure: one clarification"
    );
    assert.equal(call.step(), "ASK_PURPOSE", "still on the step — it gets its retry");

    await badTurn(call, q);
    assert.match(
      call.socket.instructions().at(-1)!,
      /STOP ASKING IT/i,
      "second failure: the step is over"
    );
    assert.equal(call.step(), "ASK_BENEFICIARY", "and the flow has moved on");
    call.end();
  });

  it("never returns to an abandoned step", async () => {
    // The failure this guards: the flow is DERIVED from captured slots, and an
    // abandoned slot is not captured — so without the skip set, the very next
    // turn recomputes the same step and hands back the question she just gave
    // up on, forever.
    const call = await startCall();
    for (let i = 0; i < 2; i++) await badTurn(call, "What's got you looking into this?");
    assert.equal(call.step(), "ASK_BENEFICIARY");

    for (let i = 0; i < 2; i++) await badTurn(call, "Who are you protecting?");
    assert.equal(call.step(), "ASK_DISPOSITION");

    assert.deepEqual(
      [...call.session().skippedSteps],
      ["ASK_PURPOSE", "ASK_BENEFICIARY"],
      "both abandoned steps stay closed"
    );
    call.end();
  });

  it("tells the model where the call is, and what is closed", async () => {
    const call = await startCall();
    call.asks("Who are you mainly trying to make sure is protected?");
    await call.answers("My daughter, Marie.");
    await call.records("beneficiary_name", "Marie", "My daughter, Marie.");

    const block = call.socket.lastStateBlock();
    assert.match(block, /CURRENT STEP: ASK_PURPOSE/);
    assert.match(block, /ALREADY RECORDED[^\n]*ASK_BENEFICIARY/);
    assert.match(block, /never ask these again/i);
    call.end();
  });

  it("puts abandoned steps in front of the model as closed", async () => {
    const call = await startCall();
    for (let i = 0; i < 2; i++) {
      call.asks("What's got you looking into this?");
      await call.answers("[background noise]");
    }
    const block = call.socket.lastStateBlock();
    assert.match(block, /ABANDONED[^\n]*ASK_PURPOSE/);
    assert.match(block, /Do not return to them/i);
    call.end();
  });
});

describe("the flow does not disagree with the quote gate", () => {
  it("reports READY_TO_QUOTE rather than inventing its own health rule", () => {
    // ASK_HEALTH defers to tier1HealthComplete, which is what missingForQuote
    // reads. Two definitions of "health is done" would let the flow keep
    // asking screening questions after the quote gate had already opened.
    const session = {
      captured: {
        coverage_purpose: "final expenses",
        beneficiary_name: "Marie",
        burial_or_cremation: "burial",
        affordable_monthly_budget: 60,
        rating_sex: "female",
        tobacco: "none",
      },
    } as unknown as SessionState;
    const lead = makeLead(); // empty acquisitionPlan → tier-1 health complete
    assert.equal(currentStepId(session, lead, new Set()), "READY_TO_QUOTE");
    const block = discoveryStateBlock(session, lead, new Set());
    assert.match(block, /Discovery is complete/i);
    assert.match(block, /get_quote/);
  });
});
