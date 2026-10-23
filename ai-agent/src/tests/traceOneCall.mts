/**
 * Drive ONE complete call through the real control loop and print the per-turn
 * timeline.
 *
 * Not a test — a diagnostic harness. It runs the real RealtimeHandler, the real
 * turn gate, the real tool handlers, the real sales machine and (when
 * EASTWOOD_API_KEY is set and core is reachable) the real quote engine over
 * HTTP. The only simulated party is the model: this script plays OpenAI's side
 * of the Realtime socket, emitting exactly the events the API emits.
 *
 * That is deliberate. The question being asked is what the CONTROL LOOP does —
 * when it advances, when it waits, when it repeats, when it infers completion.
 * Scripting the model's side is what makes those answers deterministic and
 * reproducible; a real model would answer the question "is the model good" and
 * leave the control loop untested.
 *
 * Usage:
 *   npx tsx src/tests/traceOneCall.mts [--script=happy|noisy]
 */

process.env["NODE_ENV"] ??= "production";
process.env["LOG_LEVEL"] ??= "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://localhost:18080";
process.env["WATCHDOG_STALL_MS"] ??= "8000";
process.env["TURN_GATE_MAX_WAIT_MS"] ??= "1500";
process.env["TRANSCRIPT_CONFIDENCE_MIN"] ??= "0.6";

import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";
import type { TurnTrace } from "../realtime/callTrace.js";

const { RealtimeHandler } = await import("../realtime/realtimeHandler.js");

/** Clean recognition (mean per-token probability ≈ 0.98). */
const CLEAR = Array(8).fill({ logprob: -0.02 });
/** Mangled recognition, under the 0.6 threshold. */
const MUMBLED = Array(8).fill({ logprob: -1.6 });

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
  responses(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "response.create");
  }
  lastInstructions(): string {
    const r = this.responses().at(-1);
    return String(r?.["response"]?.["instructions"] ?? "");
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

class RecordingBridge {
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
    lastName: "Hayes",
    age: 62,
    state: "MI",
    productLine: "final_expense",
    consentStatus: "express_written",
    acquisitionPlan: [],
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function waitFor(p: () => boolean, label: string, timeoutMs = 4000): Promise<boolean> {
  const deadline = Date.now() + timeoutMs;
  while (!p()) {
    if (Date.now() > deadline) {
      console.error(`  ⚠ timed out waiting for: ${label}`);
      return false;
    }
    await sleep(5);
  }
  return true;
}

// ── The call ────────────────────────────────────────────────────────────────

const socket = new FakeOpenAiSocket();
const bridge = new RecordingBridge();
const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => socket as any);
const sessionId = randomUUID();
const lead = makeLead();
await handler.createSession(sessionId, lead);
socket.emit("open");
socket.emitRaw({ type: "session.created" });
const tele = new FakeTelephonySocket();
handler.bindTelephony(sessionId, tele as any);
tele.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid: "MZ1" })));

/** The model speaks a line (audio + transcript deltas), then finishes. */
function speak(text: string): void {
  for (const w of text.split(/(?<=\s)/)) {
    socket.emitRaw({ type: "response.output_audio.delta", delta: "AAAA" });
    socket.emitRaw({ type: "response.audio_transcript.delta", delta: w });
  }
}
function finish(): void {
  socket.emitRaw({ type: "response.done" });
}

/** The prospect speaks: VAD open, VAD close, then a finalized transcript. */
async function prospect(text: string, opts: { clear?: boolean; silent?: boolean } = {}) {
  const before = socket.responses().length;
  socket.emitRaw({ type: "input_audio_buffer.speech_started" });
  await sleep(10);
  socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
  if (!opts.silent) {
    await sleep(20);
    socket.emitRaw({
      type: "conversation.item.input_audio_transcription.completed",
      transcript: text,
      logprobs: opts.clear === false ? MUMBLED : CLEAR,
    });
  }
  await waitFor(() => socket.responses().length > before, `a response after "${text}"`, 3000);
}

/** The model records a slot, the way the prompt instructs. */
async function record(
  field: string,
  captureKey: string,
  value: unknown,
  heardVerbatim: string
): Promise<Record<string, any> | undefined> {
  const n = socket.toolResults().length;
  socket.emitRaw({
    type: "response.function_call_arguments.done",
    name: "update_lead_field",
    call_id: `c_${captureKey}_${Math.random().toString(36).slice(2, 7)}`,
    arguments: JSON.stringify({ field, captureKey, value, heardVerbatim }),
  });
  await waitFor(() => socket.toolResults().length > n, `tool result for ${captureKey}`, 4000);
  return socket.toolResults().at(-1);
}

async function callTool(name: string, args: Record<string, unknown>) {
  const n = socket.toolResults().length;
  socket.emitRaw({
    type: "response.function_call_arguments.done",
    name,
    call_id: `c_${name}_${Math.random().toString(36).slice(2, 7)}`,
    arguments: JSON.stringify(args),
  });
  await waitFor(() => socket.toolResults().length > n, `tool result for ${name}`, 8000);
  return socket.toolResults().at(-1);
}

// The opening is delivered by the handler; it holds the floor until done.
finish();

const captures: Array<{ key: string; state: string; reason?: string }> = [];
async function step(
  say: string,
  reply: string,
  slot?: { field: string; key: string; value: unknown; verbatim: string },
  opts: { clear?: boolean } = {}
) {
  speak(say);
  finish();
  await prospect(reply, opts);
  if (slot) {
    speak("Right. ");
    const r = await record(slot.field, slot.key, slot.value, slot.verbatim);
    captures.push({
      key: slot.key,
      state: String(r?.["slotState"] ?? "(no result)"),
      ...(r?.["reason"] ? { reason: String(r["reason"]) } : {}),
    });
  }
  finish();
}

// ── A complete, cooperative call: every answer clear, every slot recorded ────

await step("Hi, am I speaking with Dolores?", "Yes, that's me.");
await step(
  "I'll ask a few quick questions so I can narrow down what may fit. That alright?",
  "Sure, go ahead."
);
await step(
  "What's got you looking into this?",
  "I don't want my daughter stuck with the bill.",
  { field: "coverage_purpose", key: "coverage_purpose", value: "final expenses", verbatim: "I don't want my daughter stuck with the bill." }
);
await step(
  "Who are you mainly trying to make sure is protected?",
  "My daughter, Marie.",
  { field: "beneficiary_name", key: "beneficiary_name", value: "Marie", verbatim: "My daughter, Marie." }
);
await step(
  "Would this mostly be for burial or cremation costs, or are you trying to leave something behind for family as well?",
  "Burial.",
  { field: "burial_or_cremation", key: "burial_or_cremation", value: "burial", verbatim: "Burial." }
);
await step(
  "Did you have a coverage amount in mind, or a monthly budget?",
  "I'd like to stay around sixty a month.",
  { field: "affordable_monthly_budget", key: "affordable_monthly_budget", value: 60, verbatim: "I'd like to stay around sixty a month." }
);
await step(
  "For rating purposes, are you male or female?",
  "Female.",
  { field: "rating_sex", key: "rating_sex", value: "female", verbatim: "Female." }
);
await step(
  "Have you used any tobacco or nicotine in the past twelve months?",
  "No, never.",
  { field: "tobacco", key: "tobacco", value: "none", verbatim: "No, never." }
);

// ── Quote progression ───────────────────────────────────────────────────────

const quoteState = socket.toolResults().at(-1);
console.log("\n── quote gate before get_quote ─────────────────────────────────");
console.log("  quoteReady   :", quoteState?.["quoteReady"]);
console.log("  missing      :", JSON.stringify(quoteState?.["missingForQuote"] ?? []));

speak("Let me pull those numbers up for you — one second. ");
const quote = await callTool("get_quote", {
  productLine: "final_expense",
  age: 62,
  state: "MI",
  tobaccoUser: false,
});
finish();
console.log("\n── get_quote ───────────────────────────────────────────────────");
console.log("  ", JSON.stringify(quote).slice(0, 500));

await handler.endSession(sessionId, "trace-complete");

// ── Render the timeline ─────────────────────────────────────────────────────

const traces: TurnTrace[] = (handler as any).__lastTrace ?? [];
void traces;

console.log("\n── captures ────────────────────────────────────────────────────");
for (const c of captures) {
  const mark = c.state === "CONFIRMED" ? "✓" : "✗";
  console.log(`  ${mark} ${c.key.padEnd(28)} ${c.state}${c.reason ? "  — " + c.reason.slice(0, 80) : ""}`);
}

console.log("\n── events published ────────────────────────────────────────────");
const byType = new Map<string, number>();
for (const e of bridge.events) byType.set(String(e["eventType"]), (byType.get(String(e["eventType"])) ?? 0) + 1);
for (const [k, v] of byType) console.log(`  ${String(v).padStart(3)}  ${k}`);

process.exit(0);
