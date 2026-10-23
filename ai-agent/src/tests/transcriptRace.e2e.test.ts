/**
 * The capture path against the real event ordering.
 *
 * Input transcription is asynchronous: on a live call the model routinely
 * calls update_lead_field BEFORE the words that justify the capture have been
 * transcribed. Judging grounding at that instant is wrong in both directions —
 * it stores values with no evidence behind them, and it rejects answers the
 * prospect gave perfectly clearly, sending Imogen back to re-ask a question
 * that was already answered. Both are worse on a call than waiting a beat.
 *
 * These drive the handler with events in the awkward order a real call
 * produces them, rather than asserting against a pre-seeded transcript list.
 */
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["WATCHDOG_STALL_MS"] = "30000"; // not a stall test
process.env["TRANSCRIPT_WAIT_MS"] = "1500";
process.env["TRANSCRIPT_CONFIDENCE_MIN"] = "0.6";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;

class FakeOpenAiSocket extends EventEmitter {
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
  sessionUpdates(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "session.update");
  }
  /** Tool results the handler sent back to the model. */
  toolOutputs(): Array<Record<string, any>> {
    return this.sent
      .filter((m) => m["type"] === "conversation.item.create")
      .map((m) => JSON.parse(m["item"]["output"]));
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

async function waitFor(p: () => boolean, label: string, timeoutMs = 4000): Promise<void> {
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

/** The prospect speaks and stops — the turn boundary a capture is judged against. */
function prospectSpeaks(socket: FakeOpenAiSocket): void {
  socket.emitRaw({ type: "input_audio_buffer.speech_started" });
  socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
}

function transcriptArrives(
  socket: FakeOpenAiSocket,
  transcript: string,
  logprobs?: Array<{ logprob: number }>
): void {
  socket.emitRaw({
    type: "conversation.item.input_audio_transcription.completed",
    transcript,
    ...(logprobs ? { logprobs } : {}),
  });
}

function modelCaptures(
  socket: FakeOpenAiSocket,
  args: Record<string, unknown>,
  callId = "call_1"
): void {
  socket.emitRaw({
    type: "response.function_call_arguments.done",
    name: "update_lead_field",
    call_id: callId,
    arguments: JSON.stringify(args),
  });
}

const CLEAR = Array(6).fill({ logprob: -0.02 });
const MUMBLED = Array(6).fill({ logprob: -2.5 });

describe("capture waits for the turn's transcript before ruling", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
  });

  it("accepts an answer whose transcript lands AFTER the capture call", async () => {
    // The real ordering on a live call. Ruling immediately would reject this
    // and make Imogen re-ask a question the prospect answered clearly.
    const { socket } = await startCall();
    prospectSpeaks(socket);

    modelCaptures(socket, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "no I've never smoked",
    });

    // Transcription finalizes a beat later, as it does in production.
    await new Promise((r) => setTimeout(r, 60));
    assert.equal(socket.toolOutputs().length, 0, "must not rule before the words arrive");
    transcriptArrives(socket, "No, I've never smoked.", CLEAR);

    await waitFor(() => socket.toolOutputs().length === 1, "capture ruled on");
    assert.equal(socket.toolOutputs()[0]!["captured"], true);
  });

  it("rejects a value when this turn's transcript never supports it", async () => {
    // Transcription is working — an earlier turn transcribed fine — but
    // nothing the prospect said this turn backs the claim.
    const { socket } = await startCall();
    prospectSpeaks(socket);
    transcriptArrives(socket, "Hang on, someone's at the door.", CLEAR);

    modelCaptures(socket, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "no none of those",
    });

    await waitFor(() => socket.toolOutputs().length === 1, "capture ruled on");
    const out = socket.toolOutputs()[0]!;
    assert.equal(out["captured"], false);
    assert.equal(out["rejected"], true);
  });

  it("gives up waiting rather than hanging the call forever", async () => {
    // Transcription never delivers. The prospect must not be left in silence
    // while a tool result that will never come is awaited.
    const { socket } = await startCall();
    prospectSpeaks(socket);

    modelCaptures(socket, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "never smoked",
    });

    await waitFor(
      () => socket.toolOutputs().length === 1,
      "capture ruled on after the wait budget",
      6000
    );
  });

  it("holds a mumbled answer for read-back instead of storing it", async () => {
    const { socket } = await startCall();
    prospectSpeaks(socket);

    modelCaptures(socket, {
      field: "age",
      captureKey: "age",
      value: 68,
      heardVerbatim: "I'm sixty eight",
    });
    transcriptArrives(socket, "I'm sixty eight.", MUMBLED);

    await waitFor(() => socket.toolOutputs().length === 1, "capture ruled on");
    const out = socket.toolOutputs()[0]!;
    assert.equal(out["captured"], false);
    assert.equal(out["needsConfirmation"], true);
  });

  it("stores the same answer when it was heard clearly", async () => {
    const { socket } = await startCall();
    prospectSpeaks(socket);

    modelCaptures(socket, {
      field: "age",
      captureKey: "age",
      value: 68,
      heardVerbatim: "I'm sixty eight",
    });
    transcriptArrives(socket, "I'm sixty eight.", CLEAR);

    await waitFor(() => socket.toolOutputs().length === 1, "capture ruled on");
    assert.equal(socket.toolOutputs()[0]!["captured"], true);
  });
});

describe("transcription is configured to produce real evidence", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
  });

  it("requests logprobs, the only real confidence signal available", async () => {
    const { socket } = await startCall();
    const update = socket.sessionUpdates().at(-1)!;
    assert.deepEqual(update["session"]["include"], [
      "item.input_audio_transcription.logprobs",
    ]);
  });

  it("uses a transcription model that can actually return logprobs", async () => {
    // whisper-1 returns none, which would leave every capture unscored.
    const { socket } = await startCall();
    const update = socket.sessionUpdates().at(-1)!;
    const model = update["session"]["audio"]["input"]["transcription"]["model"];
    assert.match(String(model), /transcribe/, `${model} cannot return logprobs`);
  });

  it("retries without logprobs if the API rejects the field, rather than running unconfigured", async () => {
    // A rejected session.update means no instructions and no tools — far
    // worse than losing an optional score.
    const { socket } = await startCall();
    const before = socket.sessionUpdates().length;

    socket.emitRaw({
      type: "error",
      error: { message: "Unknown parameter: 'session.include'." },
    });

    await waitFor(() => socket.sessionUpdates().length > before, "session re-sent");
    const retry = socket.sessionUpdates().at(-1)!;
    assert.equal(retry["session"]["include"], undefined, "the retry must drop include");
    assert.ok(retry["session"]["instructions"], "the retry must still configure the call");
    assert.ok(retry["session"]["tools"], "the retry must still expose tools");
  });
});

describe("the pipeline reports where the time went", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
  });

  it("records speech start, first partial, and final transcript as distinct stages", async () => {
    // "The AI hears me seconds late" needs these separated: a transcriber slow
    // to START and one slow to FINISH are different faults with different fixes.
    const { socket, handler, sessionId } = await startCall();
    const timing = () => (handler as any).sessions.get(sessionId).turnTiming;

    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" }); // opens a turn
    socket.emitRaw({ type: "input_audio_buffer.speech_started" });
    socket.emitRaw({ type: "conversation.item.input_audio_transcription.delta", delta: "I'm" });
    transcriptArrives(socket, "I'm sixty eight.", CLEAR);

    const tt = timing();
    assert.ok(tt.speechStartedAt, "speech start must be timestamped");
    assert.ok(tt.firstTranscriptPartialAt, "first partial must be timestamped");
    assert.ok(tt.transcriptCompletedAt, "final transcript must be timestamped");
  });
});
