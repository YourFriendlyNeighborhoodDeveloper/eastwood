/**
 * Per-turn latency instrumentation.
 *
 * The watchdog only catches total stalls; it says nothing about a turn that
 * answers in 3 seconds — which never trips an 8s timer but absolutely reads
 * as "the AI is thinking, not conversing" on a live call. These tests prove
 * the per-turn breakdown actually records where the time went, and that the
 * silent-tool-wait case (a tool call with no spoken beat first, so the
 * prospect hears dead air while it runs) is detected rather than passing
 * silently as a "healthy" turn.
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["WATCHDOG_STALL_MS"] = "5000"; // long: these tests are not about stalls

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, beforeEach, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let silenceToFirstAudioMs: typeof import("../observability.js").silenceToFirstAudioMs;
let turnTotalMs: typeof import("../observability.js").turnTotalMs;
let silentToolWaitMs: typeof import("../observability.js").silentToolWaitMs;

class FakeOpenAiSocket extends EventEmitter {
  readyState = 1;
  readonly sent: Array<Record<string, any>> = [];
  send(raw: string): void {
    this.sent.push(JSON.parse(raw));
  }
  close(): void {
    this.readyState = 3;
  }
  emitRaw(payload: Record<string, unknown>): void {
    this.emit("message", Buffer.from(JSON.stringify(payload)));
  }
  sessionUpdates(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "session.update");
  }
}

class FakeBridge {
  readonly events: Array<Record<string, any>> = [];
  async publishEvent(e: Record<string, any>): Promise<void> {
    this.events.push(e);
  }
}

class FakeTelephonySocket extends EventEmitter {
  readyState = 1;
  readonly media: unknown[] = [];
  send(raw: string): void {
    this.media.push(JSON.parse(raw));
  }
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

async function waitFor(p: () => boolean, label: string, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!p()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

async function startCall() {
  const bridge = new FakeBridge();
  const socket = new FakeOpenAiSocket();
  const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => socket as any);
  const sessionId = randomUUID();
  await handler.createSession(sessionId, makeLead());
  socket.emit("open");
  socket.emitRaw({ type: "session.created" });
  const tele = new FakeTelephonySocket();
  handler.bindTelephony(sessionId, tele as any);
  tele.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid: "MZ1" })));
  return { socket, tele, bridge, handler, sessionId };
}

/** Total observations recorded on a histogram (bucket-independent). */
async function count(h: { get(): Promise<any> }): Promise<number> {
  const v = await h.get();
  const c = v.values.find((x: any) => x.metricName?.endsWith("_count"));
  return c ? c.value : 0;
}

describe("per-turn latency instrumentation", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
    ({ silenceToFirstAudioMs, turnTotalMs, silentToolWaitMs } = await import(
      "../observability.js"
    ));
  });
  beforeEach(() => {
    silenceToFirstAudioMs.reset();
    turnTotalMs.reset();
    silentToolWaitMs.reset();
  });

  it("records the speech-end → first-audio gap, which the watchdog never measures", async () => {
    const { socket, tele } = await startCall();

    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    await new Promise((r) => setTimeout(r, 30)); // a real, if small, gap
    socket.emitRaw({ type: "response.created" });
    socket.emitRaw({ type: "response.output_audio.delta", delta: "aGVsbG8=" });

    await waitFor(async () => (await count(silenceToFirstAudioMs)) === 1, "silence gap recorded");
    assert.equal(await count(silenceToFirstAudioMs), 1);
    assert.ok(tele.media.length > 0, "audio must actually reach telephony");
  });

  it("flags a tool call that happens with no spoken acknowledgement first", async () => {
    const { socket } = await startCall();

    // Prospect answers, and the model goes straight to a tool with nothing
    // said in between — the prospect hears silence while it runs.
    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    socket.emitRaw({
      type: "response.function_call_arguments.done",
      name: "update_lead_field",
      call_id: "call_1",
      arguments: JSON.stringify({ field: "tobacco", captureKey: "tobacco", value: "none" }),
    });

    await waitFor(async () => (await count(silentToolWaitMs)) === 1, "silent tool wait flagged");
    assert.equal(await count(silentToolWaitMs), 1);
  });

  it("does NOT flag a tool call that was preceded by speech", async () => {
    const { socket } = await startCall();

    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    // The model speaks first ("Perfect, thanks.") — this is the desired shape.
    socket.emitRaw({ type: "response.audio_transcript.delta", delta: "Perfect, thanks." });
    socket.emitRaw({
      type: "response.function_call_arguments.done",
      name: "update_lead_field",
      call_id: "call_2",
      arguments: JSON.stringify({ field: "tobacco", captureKey: "tobacco", value: "none" }),
    });

    await new Promise((r) => setTimeout(r, 60));
    assert.equal(
      await count(silentToolWaitMs),
      0,
      "speaking before the tool call is exactly what we want — it must not be flagged"
    );
  });

  it("records a total for a completed turn", async () => {
    const { socket } = await startCall();

    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    socket.emitRaw({ type: "response.created" });
    socket.emitRaw({ type: "response.output_audio.delta", delta: "aGk=" });
    socket.emitRaw({ type: "response.done" });

    await waitFor(async () => (await count(turnTotalMs)) === 1, "turn total recorded");
    assert.equal(await count(turnTotalMs), 1);
  });

  it("enables input transcription so the transcript stage is a real measurement", async () => {
    const { socket } = await startCall();
    const update = socket.sessionUpdates().at(-1)!;
    assert.ok(
      update["session"]?.["audio"]?.["input"]?.["transcription"],
      "without this the transcript stage of the breakdown is permanently blank"
    );
  });
});
