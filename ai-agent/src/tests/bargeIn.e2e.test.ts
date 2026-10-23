/**
 * Barge-in: when the prospect starts talking, the assistant stops.
 *
 * Cancelling generation upstream is NOT enough on its own. Audio deltas are
 * forwarded to Twilio as fast as the model produces them, and Twilio buffers
 * that audio and plays it back in real time — so the model can be many
 * seconds ahead of what the prospect is actually hearing. Without an explicit
 * `clear` to Twilio, the queued audio keeps playing straight over the
 * interruption, which is exactly the "it talks over me" behaviour, and it
 * also makes replies feel badly delayed because the prospect is responding to
 * audio from several seconds ago.
 */
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["WATCHDOG_STALL_MS"] = "5000";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, beforeEach, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let bargeIns: typeof import("../observability.js").bargeIns;

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
  cancels(): number {
    return this.sent.filter((m) => m["type"] === "response.cancel").length;
  }
}

class FakeBridge {
  async publishEvent(): Promise<void> {}
}

class FakeTelephonySocket extends EventEmitter {
  readyState = 1;
  readonly sent: Array<Record<string, any>> = [];
  send(raw: string): void {
    this.sent.push(JSON.parse(raw));
  }
  close(): void {
    this.readyState = 3;
  }
  clears(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["event"] === "clear");
  }
  mediaFrames(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["event"] === "media");
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
  const socket = new FakeOpenAiSocket();
  const handler = new RealtimeHandler(new FakeBridge() as unknown as KafkaBridge, () => socket as any);
  const sessionId = randomUUID();
  await handler.createSession(sessionId, makeLead());
  socket.emit("open");
  socket.emitRaw({ type: "session.created" });
  const tele = new FakeTelephonySocket();
  handler.bindTelephony(sessionId, tele as any);
  tele.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid: "MZ1" })));
  return { socket, tele, handler, sessionId };
}

/** Assistant begins speaking: audio starts flowing to Twilio. */
function assistantStartsSpeaking(socket: FakeOpenAiSocket): void {
  socket.emitRaw({ type: "response.created" });
  for (let i = 0; i < 5; i++) {
    socket.emitRaw({ type: "response.output_audio.delta", delta: "aGVsbG8=" });
  }
}

describe("barge-in", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
    ({ bargeIns } = await import("../observability.js"));
  });
  beforeEach(() => bargeIns.reset());

  it("flushes Twilio's queued audio the moment the prospect starts talking", async () => {
    const { socket, tele } = await startCall();
    assistantStartsSpeaking(socket);
    await waitFor(() => tele.mediaFrames().length >= 5, "assistant audio queued");
    assert.equal(tele.clears().length, 0, "nothing flushed while it is our turn");

    socket.emitRaw({ type: "input_audio_buffer.speech_started" });

    await waitFor(() => tele.clears().length === 1, "audio flushed on interruption");
    assert.equal(
      tele.clears()[0]!["streamSid"],
      "MZ1",
      "the clear must target the live stream, or Twilio ignores it"
    );
  });

  it("cancels generation upstream as well, so no further audio is produced", async () => {
    const { socket, tele } = await startCall();
    assistantStartsSpeaking(socket);
    await waitFor(() => tele.mediaFrames().length >= 5, "assistant audio queued");

    socket.emitRaw({ type: "input_audio_buffer.speech_started" });

    await waitFor(() => socket.cancels() === 1, "response.cancel sent");
  });

  it("does nothing when the prospect speaks and the assistant is NOT talking", async () => {
    // Their normal turn. Flushing here would cut off nothing and a stray
    // cancel would be noise against a non-existent response.
    const { socket, tele } = await startCall();

    socket.emitRaw({ type: "input_audio_buffer.speech_started" });
    await new Promise((r) => setTimeout(r, 40));

    assert.equal(tele.clears().length, 0);
    assert.equal(socket.cancels(), 0);
  });

  it("flushes once per interruption, not once per audio frame", async () => {
    const { socket, tele } = await startCall();
    assistantStartsSpeaking(socket);
    await waitFor(() => tele.mediaFrames().length >= 5, "audio queued");

    socket.emitRaw({ type: "input_audio_buffer.speech_started" });
    socket.emitRaw({ type: "input_audio_buffer.speech_started" });
    socket.emitRaw({ type: "input_audio_buffer.speech_started" });
    await new Promise((r) => setTimeout(r, 40));

    assert.equal(tele.clears().length, 1, "one interruption is one flush");
  });

  it("can be interrupted again on a later turn", async () => {
    const { socket, tele } = await startCall();

    assistantStartsSpeaking(socket);
    await waitFor(() => tele.mediaFrames().length >= 5, "first turn audio");
    socket.emitRaw({ type: "input_audio_buffer.speech_started" });
    await waitFor(() => tele.clears().length === 1, "first flush");

    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    assistantStartsSpeaking(socket);
    await waitFor(() => tele.mediaFrames().length >= 10, "second turn audio");
    socket.emitRaw({ type: "input_audio_buffer.speech_started" });

    await waitFor(() => tele.clears().length === 2, "second flush");
  });

  it("counts interruptions so barge-in frequency is visible", async () => {
    const { socket, tele } = await startCall();
    assistantStartsSpeaking(socket);
    await waitFor(() => tele.mediaFrames().length >= 5, "audio queued");
    socket.emitRaw({ type: "input_audio_buffer.speech_started" });

    await waitFor(async () => {
      const v = await bargeIns.get();
      return (v.values[0]?.value ?? 0) >= 1;
    }, "barge-in counted");
  });
});

describe("turn-detection settings guard correctness, not just pace", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
  });

  it("does not commit a turn on a brief mid-sentence pause", async () => {
    // Ordinary phone speech pauses mid-sentence ("I'm... uh... sixty-eight").
    // A short silence window commits the turn early and hands the model a
    // FRAGMENT, which it then answers as though it were the whole reply —
    // this is the mechanism behind invented answers, so the floor is
    // asserted here to stop it being tuned away for snappier pacing.
    const { socket } = await startCall();
    const update = socket.sent.filter((m) => m["type"] === "session.update").at(-1)!;
    const vad = update["session"]["audio"]["input"]["turn_detection"];

    assert.ok(
      vad["silence_duration_ms"] >= 800,
      `silence_duration_ms=${vad["silence_duration_ms"]} cuts speakers off mid-sentence`
    );
    assert.ok(
      vad["threshold"] >= 0.55,
      "a low threshold lets background noise open a turn"
    );
    assert.equal(vad["interrupt_response"], true, "barge-in must be enabled");
  });
});
