/**
 * Regression coverage for the watchdog/stall-recovery timer.
 *
 * Before this fix, nothing in the codebase detected the model going silent
 * after the prospect finished speaking — the Realtime API's server-side VAD
 * was trusted to always auto-generate a response, and when it didn't, the
 * call just sat there indefinitely (the prospect had to say "hello?" to
 * continue). This proves: (1) a stall gets nudged, (2) a nudge that produces
 * a response clears the stall, and (3) a call that never recovers is ended
 * cleanly rather than left silent forever.
 */

// Must be set before any import that reads config at module scope.
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
// Fast, deterministic stall detection for the test — the default (8000ms /
// 2 nudges) would make this test take tens of seconds for no reason.
process.env["WATCHDOG_STALL_MS"] = "40";
process.env["WATCHDOG_MAX_NUDGES"] = "1";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let watchdogStalls: typeof import("../observability.js").watchdogStalls;

class FakeOpenAiSocket extends EventEmitter {
  readyState = 1; // WebSocket.OPEN
  readonly sent: Array<Record<string, any>> = [];

  send(raw: string): void {
    this.sent.push(JSON.parse(raw));
  }
  close(): void {
    this.readyState = 3;
  }

  /** Every response.create we sent, in order (opening + any nudges). */
  responseCreates(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "response.create");
  }

  emitRaw(payload: Record<string, unknown>): void {
    this.emit("message", Buffer.from(JSON.stringify(payload)));
  }
}

class FakeBridge {
  readonly events: Array<Record<string, any>> = [];
  async publishEvent(event: Record<string, any>): Promise<void> {
    this.events.push(event);
  }
  count(eventType: string): number {
    return this.events.filter((e) => e["eventType"] === eventType).length;
  }
  find(eventType: string): Record<string, any> | undefined {
    return this.events.find((e) => e["eventType"] === eventType);
  }
}

class FakeTelephonySocket extends EventEmitter {
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
    state: "MI",
    productLine: "final_expense",
    consentStatus: "express_written",
    acquisitionPlan: [],
  };
}

async function waitFor(predicate: () => boolean, label: string, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

async function startCall(): Promise<{
  socket: FakeOpenAiSocket;
  tele: FakeTelephonySocket;
  bridge: FakeBridge;
  handler: InstanceType<RealtimeHandlerCtor>;
  sessionId: string;
}> {
  const bridge = new FakeBridge();
  const socket = new FakeOpenAiSocket();
  const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => socket as any);
  const sessionId = randomUUID();

  await handler.createSession(sessionId, makeLead());
  socket.emit("open"); // triggers session.created readiness path once we emit it below
  socket.emitRaw({ type: "session.created" });

  const tele = new FakeTelephonySocket();
  handler.bindTelephony(sessionId, tele as any);
  // Twilio's "start" event is what actually lets the opening go out.
  tele.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid: "MZ123" })));

  await waitFor(() => socket.responseCreates().length > 0, "opening response.create");
  return { socket, tele, bridge, handler, sessionId };
}

describe("watchdog stall recovery", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
    ({ watchdogStalls } = await import("../observability.js"));
  });
  beforeEach(() => {
    watchdogStalls.reset();
  });
  after(() => {});

  it("nudges the model when it goes silent after the prospect finishes speaking", async () => {
    const { socket } = await startCall();
    const before = socket.responseCreates().length;

    // The prospect answered; the model does nothing back (no response.* event
    // at all) — simulating a missed/failed server_vad auto-response.
    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });

    await waitFor(
      () => socket.responseCreates().length > before,
      "a recovery response.create after the stall timeout"
    );

    const nudge = socket.responseCreates().at(-1)!;
    assert.match(
      String(nudge["response"]?.["instructions"] ?? ""),
      /gone silent/i,
      "the nudge must instruct the model to speak now, not resend a blank response.create"
    );

    const stalls = await watchdogStalls.get();
    const nudged = stalls.values.find((v) => v.labels["outcome"] === "nudged");
    assert.ok(nudged && nudged.value >= 1, "a stall must be counted");
  });

  it("stops nudging once the model responds — no repeat nudge on a healthy call", async () => {
    const { socket } = await startCall();

    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    await waitFor(() => socket.responseCreates().length > 1, "the nudge fires");
    const afterNudge = socket.responseCreates().length;

    // The model recovers and actually responds.
    socket.emitRaw({ type: "response.output_audio.delta", delta: "abc" });
    socket.emitRaw({ type: "response.done" });

    // Wait well past the stall window again — no further nudge should appear.
    await new Promise((r) => setTimeout(r, 120));
    assert.equal(
      socket.responseCreates().length,
      afterNudge,
      "a healthy response must clear the watchdog, not just delay it"
    );

    const stalls = await watchdogStalls.get();
    const recovered = stalls.values.find((v) => v.labels["outcome"] === "recovered");
    assert.ok(recovered && recovered.value >= 1, "recovery after a stall must be counted");
  });

  it("ends the call cleanly instead of leaving the prospect on a silent line forever", async () => {
    const { socket, bridge, handler, sessionId } = await startCall();

    // The prospect answered and the model never recovers, ever — WATCHDOG_MAX_NUDGES=1
    // means: first stall → nudge, second stall (still nothing) → give up.
    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });

    await waitFor(() => bridge.count("call_ended") === 1, "the call must end rather than hang forever");

    assert.equal(handler.getSession(sessionId), null, "the session must be reclaimed");
    assert.equal(handler.activeSessionCount(), 0);

    const stalls = await watchdogStalls.get();
    const gaveUp = stalls.values.find((v) => v.labels["outcome"] === "call_ended");
    assert.ok(gaveUp && gaveUp.value >= 1, "the give-up path must be counted distinctly from a nudge");
  });
});
