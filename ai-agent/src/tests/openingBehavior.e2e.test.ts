/**
 * Outbound opening behavior.
 *
 * On a connected outbound call Imogen speaks first — she does not wait for the
 * prospect to say hello — states the life-insurance purpose in her first line,
 * and does it exactly once, only after the media stream and the Realtime session
 * are both ready.
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let openingLine: typeof import("../realtime/realtimeHandler.js").openingLine;
let buildSystemPrompt: typeof import("../prompts/buildSystemPrompt.js").buildSystemPrompt;

const HEALTH_TERMS = /tobacco|medication|nicotine|diabetes|cancer|health|condition|smoke|height|weight/i;

// ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeOpenAiSocket extends EventEmitter {
  readyState = 1;
  readonly sent: Array<Record<string, any>> = [];
  send(raw: string): void {
    this.sent.push(JSON.parse(raw));
  }
  close(): void {
    this.readyState = 3;
  }
  /** Simulate a server event (session.created / session.updated / …). */
  serverEvent(obj: Record<string, unknown>): void {
    this.emit("message", Buffer.from(JSON.stringify(obj)));
  }
  /** The opening is a response.create carrying per-response instructions. */
  openings(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "response.create" && m["response"]?.["instructions"]);
  }
  openingText(): string {
    return String(this.openings()[0]?.["response"]?.["instructions"] ?? "");
  }
}

class FakeTelephonySocket extends EventEmitter {
  readyState = 1;
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
  /** Twilio sends this once the media stream begins, carrying the streamSid. */
  startStream(streamSid = "MZ0000000000000000000000000000000"): void {
    this.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid })));
  }
}

class FakeBridge {
  async publishEvent(): Promise<void> {}
}

function makeLead(overrides: Partial<LeadContext> = {}): LeadContext {
  return {
    leadId: randomUUID(),
    tenantId: "default",
    phoneE164: "+13135550123",
    firstName: "Dolores",
    state: "MI",
    productLine: "final_expense",
    consentStatus: "express_written",
    acquisitionPlan: [],
    ...overrides,
  };
}

async function startCall(lead: LeadContext): Promise<{
  handler: InstanceType<RealtimeHandlerCtor>;
  openai: FakeOpenAiSocket;
  tele: FakeTelephonySocket;
  sessionId: string;
}> {
  const openai = new FakeOpenAiSocket();
  const handler = new RealtimeHandler(new FakeBridge() as unknown as KafkaBridge, () => openai as any);
  const sessionId = randomUUID();
  await handler.createSession(sessionId, lead);
  return { handler, openai, tele: new FakeTelephonySocket(), sessionId };
}

/** Bring a call to the point where both readiness conditions are met. */
async function connectAndReady(lead: LeadContext) {
  const ctx = await startCall(lead);
  ctx.openai.serverEvent({ type: "session.updated" }); // Realtime ready
  ctx.handler.bindTelephony(ctx.sessionId, ctx.tele as any);
  ctx.tele.startStream(); // media stream started → audio can reach the prospect
  return ctx;
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("outbound opening", () => {
  before(async () => {
    ({ RealtimeHandler, openingLine } = await import("../realtime/realtimeHandler.js"));
    ({ buildSystemPrompt } = await import("../prompts/buildSystemPrompt.js"));
  });

  it("speaks first once the media stream and Realtime session are both ready", async () => {
    const { openai } = await connectAndReady(makeLead());
    assert.equal(openai.openings().length, 1, "exactly one opening is sent");
  });

  it("does not send the opening before both signals arrive", async () => {
    // Realtime ready, but telephony never bound / stream never started.
    const { openai } = await startCall(makeLead());
    openai.serverEvent({ type: "session.updated" });
    assert.equal(openai.openings().length, 0, "no opening before the media stream starts");
  });

  it("does not send the opening before telephony is bound", async () => {
    const { handler, openai, tele, sessionId } = await startCall(makeLead());
    openai.serverEvent({ type: "session.updated" });
    // Bind but never receive the stream start.
    handler.bindTelephony(sessionId, tele as any);
    assert.equal(openai.openings().length, 0, "binding alone is not enough — audio cannot flow yet");
  });

  it("does not send the opening before the Realtime session is ready", async () => {
    const { handler, openai, tele, sessionId } = await startCall(makeLead());
    handler.bindTelephony(sessionId, tele as any);
    tele.startStream(); // stream ready, but no session.created/updated yet
    assert.equal(openai.openings().length, 0, "no opening before the model session is ready");
  });

  it("states the life-insurance purpose in the first utterance", async () => {
    const { openai } = await connectAndReady(makeLead());
    const text = openai.openingText();
    assert.match(text, /life insurance/i, "the purpose must be explicit");
  });

  it("does not begin with a health question", async () => {
    const line = openingLine(makeLead(), "Imogen");
    assert.doesNotMatch(line, HEALTH_TERMS, "the opening is a greeting, not a health question");
    assert.match(line, /^Hi, this is Imogen/);
  });

  it("uses the prospect's name when known", async () => {
    const { openai } = await connectAndReady(makeLead({ firstName: "Ryan" }));
    assert.match(openai.openingText(), /Am I speaking with Ryan\?/);
  });

  it("falls back to a permission ask when the name is unknown", async () => {
    const { openai } = await connectAndReady(makeLead({ firstName: undefined }));
    const text = openai.openingText();
    assert.match(text, /Is now an okay time\?/);
    assert.doesNotMatch(text, /Am I speaking with/);
    assert.match(text, /life insurance/i);
  });

  it("stays short — the opening line is under 20 words", () => {
    for (const lead of [makeLead(), makeLead({ firstName: undefined })]) {
      const words = openingLine(lead, "Imogen").split(/\s+/).length;
      assert.ok(words < 20, `opening was ${words} words`);
    }
  });

  it("sends exactly one opening despite duplicate readiness events", async () => {
    const { handler, openai, tele, sessionId } = await startCall(makeLead());
    // Every session.update produces a session.updated; the stream start can repeat.
    openai.serverEvent({ type: "session.created" });
    openai.serverEvent({ type: "session.updated" });
    handler.bindTelephony(sessionId, tele as any);
    tele.startStream();
    tele.startStream(); // duplicate start
    openai.serverEvent({ type: "session.updated" }); // a later tool-driven update
    assert.equal(openai.openings().length, 1, "duplicate ready/connect events must not re-open");
  });

  it("explains why questions are asked before Tier-1 health begins", () => {
    const prompt = buildSystemPrompt(makeLead(), { agencyName: "Eastwood", agentDisplayName: "Imogen" });

    // The bridge that frames the questions must be present…
    assert.match(prompt, /narrow down what coverage may fit/);
    // …and must come before the health screening protocol.
    const openingIdx = prompt.indexOf("OPENING FLOW");
    const healthIdx = prompt.indexOf("HEALTH SCREENING PROTOCOL");
    assert.ok(openingIdx !== -1 && healthIdx !== -1);
    assert.ok(openingIdx < healthIdx, "the opening/why-we-ask framing precedes health screening");
  });
});
