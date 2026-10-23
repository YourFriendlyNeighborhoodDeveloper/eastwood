/**
 * The turn gate, driven through the real handler with the event sequence a
 * live call produces.
 *
 * The production failure: Imogen asks "What's your first name?" and answers
 * herself with "Perfect, thanks." before the prospect has said anything usable.
 * The cause is upstream of every check that existed — server VAD ends a turn on
 * SOUND, and `create_response` then had the model reply to that sound with no
 * transcript in hand. Refusing to STORE the invented answer afterwards fixes
 * the record and leaves the conversation broken: the prospect has already been
 * thanked, and already been asked something else.
 *
 * So these assert on what the handler tells the MODEL to do, before it speaks:
 * on a turn with no answer it must be instructed to ask again and forbidden
 * from acknowledging or advancing; on a real answer it must be left alone.
 */
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["WATCHDOG_STALL_MS"] = "30000";  // not a stall test
process.env["TURN_GATE_MAX_WAIT_MS"] = "300";
process.env["TRANSCRIPT_CONFIDENCE_MIN"] = "0.6";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let buildSystemPrompt: typeof import("../prompts/buildSystemPrompt.js").buildSystemPrompt;

/** A clean transcription — logprobs near zero mean probability near one. */
const CLEAR = Array(6).fill({ logprob: -0.02 });
/** A mangled one, well under the 0.6 confirmation threshold. */
const MUMBLED = Array(6).fill({ logprob: -1.6 });

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
  /** Instruction overrides on every response.create, "" when there was none. */
  instructions(): string[] {
    return this.responses().map((m) => String(m["response"]?.["instructions"] ?? ""));
  }
  sessionUpdates(): Array<Record<string, any>> {
    return this.sent.filter((m) => m["type"] === "session.update");
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
  clears(): number {
    return this.sent.filter((m) => m["event"] === "clear").length;
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

interface Call {
  socket: FakeOpenAiSocket;
  tele: FakeTelephonySocket;
  handler: InstanceType<RealtimeHandlerCtor>;
  sessionId: string;
  /** Imogen asks a question and finishes speaking. */
  assistantAsks(question: string): void;
  /** VAD detects the prospect's turn starting and stopping. */
  prospectTurn(): void;
  /** A finalized input transcription for the turn just ended. */
  transcript(text: string, logprobs?: unknown[]): void;
  /** The transcriber gives up on the audio. */
  transcriptionFails(): void;
  end(): void;
}

async function startCall(): Promise<Call> {
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
  // Imogen delivers the opening and stops talking, as she does on a real call.
  // Until that lands she holds the floor, and nothing else may speak.
  socket.emitRaw({ type: "response.done" });

  return {
    socket,
    tele,
    handler,
    sessionId,
    assistantAsks(question: string) {
      for (const word of question.split(/(?<=\s)/)) {
        socket.emitRaw({ type: "response.audio_transcript.delta", delta: word });
      }
      socket.emitRaw({ type: "response.done" });
    },
    prospectTurn() {
      socket.emitRaw({ type: "input_audio_buffer.speech_started" });
      socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
    },
    transcript(text: string, logprobs?: unknown[]) {
      socket.emitRaw({
        type: "conversation.item.input_audio_transcription.completed",
        transcript: text,
        ...(logprobs ? { logprobs } : {}),
      });
    },
    transcriptionFails() {
      socket.emitRaw({
        type: "conversation.item.input_audio_transcription.failed",
        error: { message: "audio unintelligible" },
      });
    },
    end() {
      void handler.endSession(sessionId, "test");
    },
  };
}

/** Instructions the handler issued for the most recent response. */
function lastInstruction(call: Call): string {
  return call.socket.instructions().at(-1) ?? "";
}

before(async () => {
  ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
  ({ buildSystemPrompt } = await import("../prompts/buildSystemPrompt.js"));
});

describe("VAD no longer decides when the assistant speaks", () => {
  it("turns off create_response so noise cannot trigger a reply", async () => {
    // This is the whole fix in one line of session config. With it true, a
    // cough generates a response before transcription has produced a word,
    // and an audio-native model handed noise produces the most probable
    // continuation of a sales call — "Perfect, thanks" and the next question.
    const call = await startCall();
    const update = call.socket.sessionUpdates()[0]!;
    const vad = update["session"]["audio"]["input"]["turn_detection"];

    assert.equal(vad["create_response"], false);
    assert.equal(vad["interrupt_response"], true, "barge-in must still work");
    call.end();
  });

  it("says nothing at all until the turn has been ruled on", async () => {
    const call = await startCall();
    const before = call.socket.responses().length;

    call.prospectTurn();
    // No transcript yet. The gate is holding the floor.
    assert.equal(
      call.socket.responses().length,
      before,
      "the assistant must not be asked to speak before the turn is adjudicated"
    );
    call.end();
  });
});

describe("a turn with no answer never advances the call", () => {
  /** Run one question → one bad prospect turn, and return what was instructed. */
  async function badTurn(
    question: string,
    drive: (call: Call) => void
  ): Promise<{ call: Call; instruction: string }> {
    const call = await startCall();
    call.assistantAsks(question);
    const before = call.socket.responses().length;
    call.prospectTurn();
    drive(call);
    await waitFor(
      () => call.socket.responses().length > before,
      "the gate ruled and instructed the model"
    );
    return { call, instruction: lastInstruction(call) };
  }

  const CASES: Array<[string, (call: Call) => void]> = [
    ["silence — no transcript ever arrives", () => {}],
    ["an empty transcript", (c) => c.transcript("")],
    ["background noise", (c) => c.transcript("[background noise]")],
    ["a failed transcription", (c) => c.transcriptionFails()],
    ["hesitation only", (c) => c.transcript("uh...", CLEAR)],
    ["an ambiguous grunt", (c) => c.transcript("mhm", CLEAR)],
    ["a clipped fragment", (c) => c.transcript("— sorry, go ahead —", CLEAR)],
  ];

  for (const [label, drive] of CASES) {
    it(`does not fill the slot from ${label}`, async () => {
      const { call, instruction } = await badTurn("Are you a smoker?", drive);

      assert.match(instruction, /DID NOT GET AN ANSWER/i, `no gate instruction for ${label}`);
      assert.match(instruction, /Do NOT.{0,80}move on to the next question/is);
      call.end();
    });

    it(`forbids acknowledging after ${label}`, async () => {
      const { call, instruction } = await badTurn("Are you a smoker?", drive);

      // The exact utterance this whole change exists to prevent.
      assert.match(instruction, /Do NOT say "Perfect"/i);
      assert.match(instruction, /Thanks/i);
      assert.match(instruction, /nothing to acknowledge/i);
      call.end();
    });

    it(`asks for a repeat after ${label}`, async () => {
      const { call, instruction } = await badTurn("What's your first name?", drive);
      assert.match(instruction, /didn't catch that|say that|one more time|what was that/i);
      call.end();
    });

    it(`records nothing after ${label}`, async () => {
      const { call, instruction } = await badTurn("Are you a smoker?", drive);
      assert.match(instruction, /Do NOT call update_lead_field/i);
      call.end();
    });
  }

  it("asks 'was that a yes or a no' after a grunt on a yes/no question", async () => {
    // A person who half-heard you does not say "could you repeat that" — they
    // put the narrow question back. That is the difference between sounding
    // like an agent and sounding like an IVR.
    const { call, instruction } = await badTurn("Are you a smoker?", (c) =>
      c.transcript("mhm", CLEAR)
    );
    assert.match(instruction, /yes or a no/i);
    call.end();
  });

  /** One question asked and answered with nothing usable, `times` over. */
  async function failQuestion(call: Call, question: string, times: number): Promise<void> {
    for (let i = 0; i < times; i++) {
      call.assistantAsks(question);
      const before = call.socket.responses().length;
      call.prospectTurn();
      call.transcript("[background noise]");
      await waitFor(() => call.socket.responses().length > before, `gate ruling ${i + 1}`);
    }
  }

  it("gives a question exactly one retry, then abandons it", async () => {
    // The bound that makes the loop terminate. Ask, listen, ONE clarification,
    // listen — and if it is still unusable the question is over. A third
    // "sorry, could you repeat that" is the point where the prospect concludes
    // she cannot hear them, and it is what a traced call actually produced:
    // five in a row with no exit.
    const call = await startCall();

    await failQuestion(call, "Are you a smoker?", 1);
    assert.match(
      lastInstruction(call),
      /YOU DID NOT GET AN ANSWER/i,
      "the first failure gets the one clarification"
    );

    await failQuestion(call, "Are you a smoker?", 1);
    const second = lastInstruction(call);
    assert.match(second, /STOP ASKING IT/i, "the second failure must abandon the question");
    assert.match(second, /Do NOT ask it again/i);
    assert.match(second, /ask the NEXT item/i, "abandoning is not stopping — she moves on");
    call.end();
  });

  it("never asks a third time, however long the call goes on", async () => {
    // The property, stated directly: no question ever gets a third attempt.
    const call = await startCall();
    await failQuestion(call, "Are you a smoker?", 6);
    const asks = call.socket
      .instructions()
      .filter((i) => /YOU DID NOT GET AN ANSWER/i.test(i)).length;
    const skips = call.socket.instructions().filter((i) => /STOP ASKING IT/i.test(i)).length;
    assert.ok(skips >= 1, "questions must actually be abandoned");
    assert.ok(
      asks <= skips + 1,
      `each abandoned question may cost at most one retry — got ${asks} retries for ${skips} skips`
    );
    call.end();
  });

  it("names the line problem once several questions in a row are abandoned", async () => {
    // Skipping one question is normal. Skipping three in a row means the
    // problem is the audio, not the question, and working the agenda is a
    // waste of the prospect's time.
    const call = await startCall();
    for (const q of ["Are you a smoker?", "What's your first name?", "How old are you?"]) {
      await failQuestion(call, q, 2);
    }
    const instr = lastInstruction(call);
    assert.match(instr, /line/i);
    assert.match(instr, /call you back|licensed agent/i, "she must offer a way out");
    assert.match(instr, /Do NOT ask another agenda question/i);
    call.end();
  });

  it("carries the outstanding question into the clarification", async () => {
    const { call, instruction } = await badTurn("What's your first name?", (c) =>
      c.transcript("[background noise]")
    );
    assert.match(instruction, /first name/i, "she must know what she was asking");
    call.end();
  });
});

describe("a real answer is left alone", () => {
  async function goodTurn(question: string, answer: string, logprobs = CLEAR): Promise<Call> {
    const call = await startCall();
    call.assistantAsks(question);
    const before = call.socket.responses().length;
    call.prospectTurn();
    call.transcript(answer, logprobs);
    await waitFor(() => call.socket.responses().length > before, "the gate let the turn through");
    return call;
  }

  it("does not override the system prompt on a clear answer", async () => {
    // The override exists for turns with exactly one valid utterance. On a
    // normal turn it would strip the sales script, the persona, and the
    // compliance rails for that response.
    const call = await goodTurn("Are you a smoker?", "No, never have.");
    assert.equal(lastInstruction(call), "", "a good answer gets the ordinary turn");
    call.end();
  });

  it("lets a qualified answer through without a repeat", async () => {
    for (const answer of ["I quit.", "I smoke occasionally.", "Used to, years ago."]) {
      const call = await goodTurn("Are you a smoker?", answer);
      assert.equal(
        lastInstruction(call),
        "",
        `a valid qualified answer was sent back for a repeat: ${answer}`
      );
      call.end();
    }
  });

  it("reads back a poorly-heard answer instead of making them start over", async () => {
    // There IS a candidate here, so one "is that right?" beats making the
    // prospect say the whole thing again — but it still may not be recorded
    // or acknowledged.
    const call = await goodTurn("How old are you?", "I'm sixty eight.", MUMBLED);
    const instruction = lastInstruction(call);

    assert.match(instruction, /NOT CLEARLY ENOUGH/i);
    assert.match(instruction, /Read back/i);
    assert.match(instruction, /Do NOT say "Perfect"/i);
    assert.match(instruction, /Do NOT move on to the next question/i);
    call.end();
  });
});

describe("one question per turn — nothing is queued while they speak", () => {
  it("does not start a second response while one is generating", async () => {
    const call = await startCall();
    const base = call.socket.responses().length;

    // One turn gets the floor and the model begins speaking (no response.done).
    call.prospectTurn();
    call.transcript("Yes, speaking.", CLEAR);
    await waitFor(() => call.socket.responses().length === base + 1, "first response");

    // A second turn lands while she is still mid-utterance.
    call.prospectTurn();
    call.transcript("Sorry, go on.", CLEAR);
    await new Promise((r) => setTimeout(r, 50));

    assert.equal(
      call.socket.responses().length,
      base + 1,
      "a second response was started on top of one already generating"
    );
    call.end();
  });

  it("releases the floor once the response finishes, not before", async () => {
    const call = await startCall();
    const base = call.socket.responses().length;

    call.prospectTurn();
    call.transcript("Yes, speaking.", CLEAR);
    await waitFor(() => call.socket.responses().length === base + 1, "first response");

    // She finishes. The parked turn may now speak — one at a time, in order.
    call.socket.emitRaw({ type: "response.done" });
    call.prospectTurn();
    call.transcript("Go ahead.", CLEAR);
    await waitFor(
      () => call.socket.responses().length === base + 2,
      "the next turn may speak once the floor is free"
    );
    call.end();
  });

  it("drops a queued response the moment the prospect starts talking", async () => {
    // Reading ahead over someone who has started speaking is the worst version
    // of this bug: they are answering the last question while she asks the next.
    const call = await startCall();
    const base = call.socket.responses().length;

    call.prospectTurn();
    call.transcript("Yes.", CLEAR);
    await waitFor(() => call.socket.responses().length === base + 1, "first response");

    // A second turn queues behind the in-flight response...
    call.prospectTurn();
    call.transcript("Actually hang on.", CLEAR);
    await new Promise((r) => setTimeout(r, 20));
    assert.equal(call.socket.responses().length, base + 1, "the second turn should be parked");

    // ...and then they start talking again before it could be sent.
    call.socket.emitRaw({ type: "input_audio_buffer.speech_started" });
    call.socket.emitRaw({ type: "response.done" });
    await new Promise((r) => setTimeout(r, 50));

    assert.equal(
      call.socket.responses().length,
      base + 1,
      "a queued response surfaced on top of the prospect"
    );
    call.end();
  });

  it("abandons a pending gate ruling if they resume speaking", async () => {
    const call = await startCall();
    const before = call.socket.responses().length;

    call.prospectTurn();                                     // gate opens
    call.socket.emitRaw({ type: "input_audio_buffer.speech_started" });  // still going
    await new Promise((r) => setTimeout(r, 400));            // past the gate budget

    assert.equal(
      call.socket.responses().length,
      before,
      "the gate ruled on a turn the prospect had not finished"
    );
    call.end();
  });
});

describe("the acknowledgement guard", () => {
  it("cuts her off if she starts thanking them on an unanswered turn", async () => {
    // Belt and braces behind the gate's instructions. A clipped syllable is
    // recoverable; "Perfect, thanks" for an answer nobody gave is not.
    const call = await startCall();
    call.assistantAsks("What's your first name?");
    const before = call.socket.responses().length;
    call.prospectTurn();
    call.transcript("[background noise]");
    await waitFor(() => call.socket.responses().length > before, "the gate ruled");

    const cancelsBefore = call.socket.cancels();
    const clearsBefore = call.tele.clears();
    // She overrides the instruction and opens with an acknowledgement anyway.
    call.socket.emitRaw({ type: "response.output_audio.delta", delta: "AAA" });
    for (const d of ["Perfect", ", thanks."]) {
      call.socket.emitRaw({ type: "response.audio_transcript.delta", delta: d });
    }

    assert.ok(call.socket.cancels() > cancelsBefore, "generation must be cancelled");
    assert.ok(call.tele.clears() > clearsBefore, "queued audio must be flushed at Twilio");
    call.end();
  });

  it("stays armed for the clarification even when it had to queue", async () => {
    // A clarification parked behind an in-flight response must carry its own
    // guard flag. Tracking it on the session instead would arm the guard
    // against the response already streaming and disarm it for the
    // clarification — precisely backwards.
    const call = await startCall();
    const base = call.socket.responses().length;

    // An ordinary turn takes the floor and keeps it.
    call.prospectTurn();
    call.transcript("Yes, that's me.", CLEAR);
    await waitFor(() => call.socket.responses().length === base + 1, "ordinary response");
    call.assistantAsks("Are you a smoker?");   // ends with response.done

    // Take the floor again so the next clarification has to queue.
    call.prospectTurn();
    call.transcript("Yes, go on.", CLEAR);
    await waitFor(() => call.socket.responses().length === base + 2, "second response");

    // Now a turn with no answer, while she is still speaking.
    call.prospectTurn();
    call.transcript("[background noise]");
    await new Promise((r) => setTimeout(r, 20));
    call.socket.emitRaw({ type: "response.done" });
    await waitFor(
      () => call.socket.instructions().at(-1)!.includes("DID NOT GET AN ANSWER"),
      "the queued clarification was sent"
    );

    // ...and the guard must still catch her acknowledging on it.
    const cancelsBefore = call.socket.cancels();
    call.socket.emitRaw({ type: "response.output_audio.delta", delta: "AAA" });
    call.socket.emitRaw({ type: "response.audio_transcript.delta", delta: "Perfect, thanks." });
    assert.ok(
      call.socket.cancels() > cancelsBefore,
      "the guard was disarmed by queueing the clarification"
    );
    call.end();
  });

  it("does not arm the guard against the response already speaking", async () => {
    // The mirror image: queueing a clarification must not cause the ordinary
    // response currently streaming to be cut off mid-acknowledgement.
    const call = await startCall();
    const base = call.socket.responses().length;

    call.prospectTurn();
    call.transcript("No, never have.", CLEAR);
    await waitFor(() => call.socket.responses().length === base + 1, "ordinary response");

    // A bad turn queues a clarification behind the in-flight ordinary response.
    call.prospectTurn();
    call.transcript("[background noise]");
    await new Promise((r) => setTimeout(r, 20));

    const cancelsBefore = call.socket.cancels();
    call.socket.emitRaw({ type: "response.output_audio.delta", delta: "AAA" });
    call.socket.emitRaw({ type: "response.audio_transcript.delta", delta: "Perfect, thanks. " });
    assert.equal(
      call.socket.cancels(),
      cancelsBefore,
      "a legitimate acknowledgement was cut off by a queued clarification"
    );
    call.end();
  });

  it("leaves an acknowledgement alone on a turn that WAS answered", async () => {
    const call = await startCall();
    call.assistantAsks("Are you a smoker?");
    const before = call.socket.responses().length;
    call.prospectTurn();
    call.transcript("No, never have.", CLEAR);
    await waitFor(() => call.socket.responses().length > before, "the gate ruled");

    const cancelsBefore = call.socket.cancels();
    call.socket.emitRaw({ type: "response.output_audio.delta", delta: "AAA" });
    call.socket.emitRaw({ type: "response.audio_transcript.delta", delta: "Perfect, thanks. " });

    assert.equal(
      call.socket.cancels(),
      cancelsBefore,
      "a legitimate acknowledgement must not be cut off"
    );
    call.end();
  });
});

describe("the system prompt states the turn contract", () => {
  const prompt = () =>
    buildSystemPrompt(makeLead(), {
      agencyName: "Eastwood Financial",
      agentDisplayName: "Imogen",
    });

  it("names the three slot states and which one permits moving on", () => {
    const p = prompt();
    assert.match(p, /UNKNOWN/);
    assert.match(p, /HEARD/);
    assert.match(p, /CONFIRMED/);
    assert.match(p, /ONLY move on when the slot is CONFIRMED/i);
  });

  it("forbids thanking someone for information it does not have", () => {
    assert.match(prompt(), /NEVER THANK SOMEONE FOR SOMETHING YOU DO NOT HAVE/i);
  });

  it("no longer tells her to acknowledge BEFORE recording the answer", () => {
    // This instruction is what produced the bug: it scripted "Perfect, thanks."
    // as the thing to say on the way into update_lead_field, so the
    // acknowledgement always preceded the verification.
    const p = prompt();
    assert.doesNotMatch(
      p,
      /Recording an answer\s*→\s*"Perfect, thanks\."/i,
      "the prompt still scripts an acknowledgement before the capture is verified"
    );
    assert.match(p, /record FIRST, then speak/i);
  });

  it("requires one question per turn, then a stop", () => {
    // \s+ between words: the prompt is hard-wrapped, so any phrase long enough
    // to be worth asserting on spans a line break.
    const p = prompt();
    assert.match(p, /ONE QUESTION PER TURN/i);
    assert.match(p, /Ask\.\s+Stop\.\s+Listen\.\s+Capture\.\s+Confirm\./i);
    assert.match(p, /Do\s+not\s+add\s+a\s+second\s+question/i);
    assert.match(p, /Do\s+not\s+read\s+ahead/i);
  });

  it("never scripts a stock acknowledgement as the thing to say after an answer", () => {
    // The prompt used to offer "Perfect, thanks." as the house acknowledgement
    // in three places, so she opened every single confirmed turn with it and
    // the call read as a script from the third question onwards. The phrase
    // survives only as a prohibition — never as an example to follow.
    const p = prompt();
    assert.doesNotMatch(
      p,
      /you may say\s*\n?\s*"Perfect, thanks"/i,
      "the prompt still hands her a stock acknowledgement to say on capture"
    );
    assert.match(p, /DO NOT ACKNOWLEDGE EVERY ANSWER/i);
    assert.match(p, /NEVER say "Perfect\."/i);
    assert.match(p, /default on a confirmed answer is NO acknowledgement/i);
  });

  it("does not tell her to run the acknowledgement into the next question unconditionally", () => {
    assert.doesNotMatch(
      prompt(),
      /same breath\s*—\s*never acknowledge and then stop/i,
      "that instruction is what produces reading ahead"
    );
  });

  it("lists what does not count as an answer", () => {
    const p = prompt();
    assert.match(p, /WHAT IS NOT AN ANSWER/i);
    for (const kind of [/background noise/i, /silence/i, /talking over each other/i, /cut off/i]) {
      assert.match(p, kind);
    }
  });

  it("still accepts a qualified answer, so the fix does not overshoot", () => {
    // A gate that makes people repeat perfectly good answers is a worse call
    // than the one it replaced.
    const p = prompt();
    assert.match(p, /WHAT IS AN ANSWER/i);
    assert.match(p, /I quit/i);
  });
});
