/**
 * Precedence: never invent an answer OUTRANKS never go silent.
 *
 * These two instructions pull against each other on a noisy line, and the
 * failure mode is specific — pressed not to leave dead air, the model fills
 * the gap with the answer it was unsure of, and a recoverable pause becomes a
 * fabricated fact on the record. A pause is recoverable and audible. An
 * invented answer is neither: nobody knows to go back and check it.
 *
 * The pace rules are the ones most likely to be tightened later in pursuit of
 * a snappier call, so the precedence is asserted here rather than left to
 * survive on wording alone.
 */
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["WATCHDOG_STALL_MS"] = "40";
process.env["WATCHDOG_MAX_NUDGES"] = "2";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

let buildSystemPrompt: typeof import("../prompts/buildSystemPrompt.js").buildSystemPrompt;
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
  /** Instruction strings from every response.create the handler sent. */
  nudges(): string[] {
    return this.sent
      .filter((m) => m["type"] === "response.create")
      .map((m) => String(m["response"]?.["instructions"] ?? ""));
  }
}

class FakeBridge {
  async publishEvent(): Promise<void> {}
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

async function waitFor(p: () => boolean, label: string, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!p()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await new Promise((r) => setTimeout(r, 5));
  }
}

describe("the system prompt ranks integrity above pace", () => {
  before(async () => {
    ({ buildSystemPrompt } = await import("../prompts/buildSystemPrompt.js"));
  });

  const prompt = () =>
    buildSystemPrompt(makeLead(), {
      agencyName: "Eastwood Financial",
      agentDisplayName: "Imogen",
    });

  it("states the never-invent rule BEFORE the never-go-silent rule", async () => {
    // Order is the precedence. If the pace rule is read first it becomes the
    // default posture, and the integrity rule reads as an exception to it.
    const p = prompt();
    const invent = p.indexOf("NEVER INVENT AN ANSWER");
    const drive = p.indexOf("YOU DRIVE EVERY TURN");

    assert.ok(invent >= 0, "the never-invent rule must be present");
    assert.ok(drive >= 0, "the drive rule must be present");
    assert.ok(invent < drive, "integrity must be stated before pace, not after it");
  });

  it("says plainly that the integrity rule outranks not being silent", async () => {
    assert.match(prompt(), /outranks.{0,80}silent/is);
  });

  it("separates making a sound from making progress", async () => {
    // The whole failure mode is reading "never go quiet" as "always advance".
    const p = prompt();
    assert.match(p, /SPEAKING AND ADVANCING ARE NOT THE SAME THING/i);
    assert.match(p, /clarifying question/i);
  });

  it("names asking them to repeat as a full, valid turn — not a failure to drive", async () => {
    assert.match(prompt(), /satisfies this rule|full, valid turn/i);
  });

  it("forbids speaking merely to fill silence", async () => {
    // \s+ throughout: the prompt is hard-wrapped, so any phrase long enough to
    // be worth asserting on spans a line break.
    assert.match(prompt(), /never\s+say\s+something\s+merely\s+because\s+saying\s+nothing\s+felt\s+wrong/i);
  });
});

describe("the watchdog nudge never pressures the model into inventing", () => {
  before(async () => {
    ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
  });

  async function nudgeText(): Promise<string> {
    const socket = new FakeOpenAiSocket();
    const handler = new RealtimeHandler(
      new FakeBridge() as unknown as KafkaBridge,
      () => socket as any
    );
    const sessionId = randomUUID();
    await handler.createSession(sessionId, makeLead());
    socket.emit("open");
    socket.emitRaw({ type: "session.created" });
    const tele = new FakeTelephonySocket();
    handler.bindTelephony(sessionId, tele as any);
    tele.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid: "MZ1" })));

    // The prospect finishes speaking and the model says nothing at all.
    socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    await waitFor(() => socket.nudges().length >= 2, "watchdog nudged");
    void handler.endSession(sessionId, "test");
    return socket.nudges().at(-1)!;
  }

  it("offers asking them to repeat as a valid way to break the silence", async () => {
    // The model often goes quiet BECAUSE it did not catch the answer. The
    // recovery prompt must not assume the opposite.
    const nudge = await nudgeText();
    assert.match(nudge, /catch that|say it again|repeat/i);
  });

  it("does not tell the model to press on with the next question regardless", async () => {
    const nudge = await nudgeText();
    assert.match(nudge, /do NOT guess/i);
    assert.match(nudge, /do NOT move on to the next question/i);
  });

  it("never forbids admitting it did not hear them", async () => {
    // The previous nudge said "continue naturally as if there was no gap",
    // which ruled out the one honest recovery available.
    const nudge = await nudgeText();
    assert.doesNotMatch(
      nudge,
      /as if there was no gap/i,
      "papering over the gap is what produces an invented answer"
    );
  });
});
