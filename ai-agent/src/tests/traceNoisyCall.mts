/**
 * Drive one call under the conditions the REAL call log shows, and trace it.
 *
 * The live call (session d600c61c, 208s) produced transcripts like "Bye-bye.",
 * "Thank you." and "Good area" — whisper's output on near-silence and line
 * noise, not words the prospect said. It never captured a slot that stuck, and
 * it never reached a quote. This reproduces that shape: silence, hallucinated
 * transcripts, backchannels, and low-confidence recognition, interleaved with
 * genuine answers.
 *
 * The four questions it is built to answer:
 *   1. does the control loop ever advance without waiting for evidence?
 *   2. does the recovery loop repeat without yielding?
 *   3. is slot completion ever inferred rather than heard?
 *   4. where does quote progression become impossible?
 */

process.env["NODE_ENV"] ??= "production";
process.env["LOG_LEVEL"] ??= "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://localhost:18080";
process.env["WATCHDOG_STALL_MS"] ??= "1500";
process.env["WATCHDOG_MAX_NUDGES"] ??= "2";
process.env["TURN_GATE_MAX_WAIT_MS"] ??= "300";
process.env["TURN_GATE_ESCALATE_AFTER"] ??= "3";
process.env["TRANSCRIPT_CONFIDENCE_MIN"] ??= "0.6";

import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

const { RealtimeHandler } = await import("../realtime/realtimeHandler.js");

const CLEAR = Array(8).fill({ logprob: -0.02 });
const MUMBLED = Array(8).fill({ logprob: -1.6 });

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

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

const socket = new Sock();
const handler = new RealtimeHandler(new Bridge() as unknown as KafkaBridge, () => socket as any);
const sessionId = randomUUID();
const lead: LeadContext = {
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
await handler.createSession(sessionId, lead);
socket.emit("open");
socket.emitRaw({ type: "session.created" });
const tele = new Tele();
handler.bindTelephony(sessionId, tele as any);
tele.emit("message", Buffer.from(JSON.stringify({ event: "start", streamSid: "MZ1" })));

function speak(text: string): void {
  socket.emitRaw({ type: "response.created" });
  for (const w of text.split(/(?<=\s)/)) {
    socket.emitRaw({ type: "response.output_audio.delta", delta: "AAAA" });
    socket.emitRaw({ type: "response.audio_transcript.delta", delta: w });
  }
}
const finish = () => socket.emitRaw({ type: "response.done" });

/** What the handler told the model to say on its most recent response. */
function lastInstruction(): string {
  return String(socket.responses().at(-1)?.["response"]?.["instructions"] ?? "");
}

interface Probe {
  label: string;
  transcript: string | null;
  clear?: boolean;
}

const observed: Array<{
  label: string;
  said: string;
  responded: boolean;
  clarification: boolean;
  instruction: string;
  instructionOpening: string;
}> = [];

/** One prospect turn under the given condition, then read what came back. */
async function turn(p: Probe): Promise<void> {
  const before = socket.responses().length;
  socket.emitRaw({ type: "input_audio_buffer.speech_started" });
  await sleep(5);
  socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
  if (p.transcript !== null) {
    await sleep(10);
    socket.emitRaw({
      type: "conversation.item.input_audio_transcription.completed",
      transcript: p.transcript,
      logprobs: p.clear === false ? MUMBLED : CLEAR,
    });
  }
  // The gate's full budget plus slack: a turn with no transcript must still
  // produce a response, from the deadline path.
  await sleep(500);
  const responded = socket.responses().length > before;
  const instr = responded ? lastInstruction() : "";
  observed.push({
    label: p.label,
    said: p.transcript === null ? "(silence — no transcript)" : p.transcript,
    responded,
    clarification: instr !== "",
    instruction: instr,
    instructionOpening: instr.split("\n").find((l) => /^YOU /.test(l)) ?? "",
  });
  if (responded) {
    speak("(model replies)");
    finish();
  }
}

finish(); // the opening lands

speak("Would this mostly be for burial or cremation costs?");
finish();

// ── The conditions from the live log, in sequence ───────────────────────────
await turn({ label: "silence, nothing transcribed", transcript: null });
await turn({ label: "whisper hallucination on silence", transcript: "Thank you." });
await turn({ label: "whisper hallucination on silence", transcript: "Bye-bye." });
await turn({ label: "backchannel grunt", transcript: "Mhm." });
await turn({ label: "line noise", transcript: "[BLANK_AUDIO]" });
await turn({ label: "prospect asks us something", transcript: "Sorry, what?" });
await turn({ label: "low-confidence recognition", transcript: "Burial.", clear: false });
await turn({ label: "a real, clear answer", transcript: "Burial." });

console.log("\n== RECOVERY LOOP =================================================");
let run = 0;
let maxRun = 0;
let skips = 0;
for (const o of observed) {
  // RETRY and SKIP are both instruction overrides; only RETRY re-asks. The
  // whole point of the bound is that a run of RETRYs cannot happen, so they
  // have to be counted separately or the output looks identical either way.
  const kind = !o.responded
    ? "STALL  "
    : /STOP ASKING IT/.test(o.instruction)
      ? "SKIP   "
      : /Do NOT ask another agenda question/.test(o.instruction)
        ? "ESCALATE"
        : o.clarification
          ? "RETRY  "
          : "ADVANCE";
  if (kind === "RETRY  ") {
    run += 1;
    maxRun = Math.max(maxRun, run);
  } else run = 0;
  if (kind === "SKIP   ") skips += 1;
  console.log(`  ${kind}  ${o.label.padEnd(34)} said=${JSON.stringify(o.said).padEnd(28)}`);
  if (o.instructionOpening) console.log(`            └─ ${o.instructionOpening}`);
}
console.log(`\n  longest unbroken run of RE-ASKS:        ${maxRun}   (contract: at most 1)`);
console.log(`  questions abandoned rather than looped: ${skips}`);
console.log(`  turns that produced no response at all: ${observed.filter((o) => !o.responded).length}`);

// ── Did anything get captured from a turn the gate rejected? ────────────────
console.log("\n== SLOT COMPLETION ===============================================");
for (const claim of ["Thank you.", "Bye-bye.", "Burial."]) {
  const n = socket.toolResults().length;
  socket.emitRaw({
    type: "response.function_call_arguments.done",
    name: "update_lead_field",
    call_id: `c_${Math.random().toString(36).slice(2, 7)}`,
    arguments: JSON.stringify({
      field: "burial_or_cremation",
      captureKey: "burial_or_cremation",
      value: "burial",
      heardVerbatim: claim,
    }),
  });
  const deadline = Date.now() + 4000;
  while (socket.toolResults().length === n && Date.now() < deadline) await sleep(5);
  const r = socket.toolResults().at(-1) ?? {};
  console.log(
    `  claim ${JSON.stringify(claim).padEnd(14)} -> ${String(r["slotState"] ?? "?").padEnd(10)} captured=${r["captured"]}`
  );
  if (r["reason"]) console.log(`      ${String(r["reason"]).slice(0, 90)}`);
}

await handler.endSession(sessionId, "trace-noisy");
process.exit(0);
