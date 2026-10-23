/**
 * Either/or questions are answered by one of the alternatives, not by a yes.
 *
 * The production failure: Imogen asks "would you prefer burial or cremation?",
 * the prospect says "burial", and the call stops there. She never reaches the
 * quote and never books — she just keeps coming back to the same question.
 *
 * The cause is question classification. Every choice question she asks opens
 * with an auxiliary or a copula ("would you prefer...", "are you male or
 * female?"), so classifyQuestion read it as a yes/no. "burial" contains no yes
 * and no no, so answersExpectation said it did not answer the question, and
 * both consumers of that then did the wrong thing with a perfectly good answer:
 *
 *   - the turn gate ruled the turn `heard_other` — real words, but not an
 *     answer — leaving the question outstanding;
 *   - update_lead_field held the slot at HEARD instead of CONFIRMED and told
 *     her to read it back with "Sorry — was that a yes or a no?", which the
 *     prospect cannot answer either, because it was never a yes/no question.
 *
 * Nothing breaks that loop: the slot never reaches CONFIRMED, so it never
 * reaches `captured`, so quoteUnlocked stays false and get_quote stays locked
 * forever. Two of the slots on the way to a quote are choice questions
 * (burial-or-cremation, rating sex), so the call cannot finish.
 *
 * The second half of the same failure is the pending question outliving the
 * answer — see the last test.
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["WATCHDOG_STALL_MS"] = "30000";
process.env["TURN_GATE_MAX_WAIT_MS"] = "300";
process.env["TRANSCRIPT_CONFIDENCE_MIN"] = "0.6";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type TurnGate = typeof import("../discovery/turnGate.js");
let classifyQuestion: TurnGate["classifyQuestion"];
let answersExpectation: TurnGate["answersExpectation"];
let adjudicateTurn: TurnGate["adjudicateTurn"];
let clarificationLine: TurnGate["clarificationLine"];
let quoteSupportsChoice: TurnGate["quoteSupportsChoice"];

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;

/** A clean transcription — logprobs near zero mean probability near one. */
const CLEAR = Array(6).fill({ logprob: -0.02 });

before(async () => {
  ({ classifyQuestion, answersExpectation, adjudicateTurn, clarificationLine, quoteSupportsChoice } = await import(
    "../discovery/turnGate.js"
  ));
  ({ RealtimeHandler } = await import("../realtime/realtimeHandler.js"));
});

describe("an either/or question is not a yes/no question", () => {
  it("classifies the choice questions on the path to a quote", () => {
    // burial-or-cremation and rating sex are both required before get_quote
    // unlocks. Misreading either as a yes/no is what stalls the call.
    for (const q of [
      "Would you prefer burial or cremation?",
      "Do you want burial or cremation?",
      "Were you thinking burial or cremation?",
      "For rating purposes, are you male or female?",
      "Is it Type 1 or Type 2?",
      "That helps. And is it Type 1 or Type 2?",
      "Which would you rather, term or whole life?",
      "Would you like the morning or the afternoon?",
    ]) {
      assert.equal(classifyQuestion(q), "choice", `should be a choice: ${q}`);
    }
  });

  it("still reads a genuine yes/no as a yes/no, so the fix does not overshoot", () => {
    // Every one of these contains "or". None of them is a choice — the "or"
    // is inside the thing being asked about, not offering alternatives. A
    // grunt must still fail these.
    for (const q of [
      "Are you a smoker?",
      "Do you have any children or grandchildren?",
      "Quick one first — any tobacco or nicotine in the past twelve months?",
      "Do you have any coverage in place, or not?",
      "Was that a yes or a no?",
      "Just to make sure I've got it right — was that a yes or a no?",
      "So that's a no on the tobacco — have I got that right?",
      "Have you been diagnosed with or treated for any of the following in the last 5 years: cancer, heart disease or heart attack, diabetes, stroke, COPD, or kidney disease?",
    ]) {
      assert.equal(classifyQuestion(q), "yes_no", `should stay yes/no: ${q}`);
    }
  });

  it("leaves the other question shapes alone", () => {
    assert.equal(classifyQuestion("What's your first name?"), "name");
    assert.equal(classifyQuestion("How much were you thinking per month?"), "amount");
    assert.equal(classifyQuestion("How old are you?"), "number");
    assert.equal(classifyQuestion("What made you look into coverage?"), "free_text");
    assert.equal(classifyQuestion("Let me get that recorded."), "none");
  });

  it("takes an alternative as the answer, and still rejects a grunt", () => {
    for (const said of ["burial", "Cremation.", "Burial.", "the morning"]) {
      assert.ok(answersExpectation(said, "choice"), `should answer a choice: ${said}`);
    }
    // The reason the gate exists is unchanged: noise is not an answer to a
    // choice question any more than to a yes/no one.
    for (const said of ["uh", "mhm", "um..."]) {
      assert.ok(!answersExpectation(said, "choice"), `must not answer a choice: ${said}`);
    }
  });

  it("rules a named alternative a real answer rather than heard_other", () => {
    const verdict = adjudicateTurn({
      transcript: "Burial.",
      confidence: 0.98,
      transcriptionFailed: false,
      expectation: "choice",
      confidenceMin: 0.6,
    });
    assert.equal(verdict.outcome, "heard_answer");
  });

  it("never asks for a yes or a no when the question offered neither", () => {
    // The line that made the loop inescapable: the prospect is asked for a
    // yes/no to a question that had no yes and no no in it.
    const line = clarificationLine("backchannel_only", "choice");
    assert.doesNotMatch(line, /yes or (a )?no/i);
    assert.match(line, /which/i);
  });
});

// ── The same failure, driven through the real handler ────────────────────────

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
  /** Every function_call_output we handed back, parsed. */
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

interface Call {
  socket: FakeOpenAiSocket;
  handler: InstanceType<RealtimeHandlerCtor>;
  sessionId: string;
  says(text: string): void;
  finishesSpeaking(): void;
  prospectSays(text: string): Promise<void>;
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
  socket.emitRaw({ type: "response.done" }); // the opening lands, floor released

  return {
    socket,
    handler,
    sessionId,
    says(text: string) {
      for (const word of text.split(/(?<=\s)/)) {
        socket.emitRaw({ type: "response.audio_transcript.delta", delta: word });
      }
    },
    finishesSpeaking() {
      socket.emitRaw({ type: "response.done" });
    },
    async prospectSays(text: string) {
      const before = socket.responses().length;
      socket.emitRaw({ type: "input_audio_buffer.speech_started" });
      socket.emitRaw({ type: "input_audio_buffer.speech_stopped" });
      socket.emitRaw({
        type: "conversation.item.input_audio_transcription.completed",
        transcript: text,
        logprobs: CLEAR,
      });
      await waitFor(() => socket.responses().length > before, `the gate rules on "${text}"`);
    },
    end() {
      void handler.endSession(sessionId, "test");
    },
  };
}

describe("the burial-or-cremation slot actually confirms", () => {
  it("records the answer instead of asking for a yes or a no forever", async () => {
    const call = await startCall();

    call.says("Would you prefer burial or cremation?");
    call.finishesSpeaking();
    await call.prospectSays("Burial.");

    // She records what she heard, quoting the prospect verbatim as the tool
    // requires.
    call.says("Let me get that down. ");
    call.socket.emitRaw({
      type: "response.function_call_arguments.done",
      name: "update_lead_field",
      call_id: "call_burial",
      arguments: JSON.stringify({
        field: "burial_or_cremation",
        captureKey: "burial_or_cremation",
        value: "burial",
        heardVerbatim: "Burial.",
      }),
    });
    await waitFor(() => call.socket.toolResults().length === 1, "the capture is ruled on");

    const result = call.socket.toolResults()[0]!;
    assert.equal(
      result["slotState"],
      "CONFIRMED",
      `the answer was clear and it answered the question, so it must confirm — got: ${JSON.stringify(result)}`
    );
    assert.equal(result["captured"], true);
    // The read-back that could never be satisfied.
    assert.ok(
      !JSON.stringify(result).match(/yes or a no/i),
      "must not ask for a yes/no on a question that offered neither"
    );
    call.end();
  });

  it("stops treating the question as outstanding once it is answered", async () => {
    // Second half of the loop. The turn that RECORDS an answer usually asks
    // nothing ("Let me get that down."), so notePendingQuestion leaves the
    // answered question standing. The prospect then makes any sound while she
    // works — and the gate judges it against a question they already answered,
    // so she asks for it again.
    const call = await startCall();

    call.says("Would you prefer burial or cremation?");
    call.finishesSpeaking();
    await call.prospectSays("Burial.");

    call.says("Let me get that down. ");
    call.socket.emitRaw({
      type: "response.function_call_arguments.done",
      name: "update_lead_field",
      call_id: "call_burial",
      arguments: JSON.stringify({
        field: "burial_or_cremation",
        captureKey: "burial_or_cremation",
        value: "burial",
        heardVerbatim: "Burial.",
      }),
    });
    await waitFor(() => call.socket.toolResults().length === 1, "the capture is ruled on");
    // Her recording turn ends, and the tool result is handed back to her. She
    // takes that turn and says nothing new in it — so it asks no question, and
    // nothing updates what the outstanding one is.
    call.finishesSpeaking();
    await waitFor(() => call.socket.responses().length >= 3, "she is asked to reply to the tool");
    call.finishesSpeaking();

    // They fill the silence she left while the tool ran.
    await call.prospectSays("Mhm.");

    const instruction = String(
      call.socket.responses().at(-1)?.["response"]?.["instructions"] ?? ""
    );
    assert.doesNotMatch(
      instruction,
      /burial or cremation/i,
      "she must not re-ask a question she has already recorded the answer to"
    );
    call.end();
  });
});

// ── Capture integrity for choice questions ──────────────────────────────────
//
// Found by tracing a call, not by reading code. Widening `choice` to accept any
// real word fixed the loop but removed the only thing checking that the words
// quoted had anything to do with the value being stored — see
// quoteSupportsChoice.

describe("a choice capture must quote words that contain the value", () => {
  it("rejects a quote that does not name the alternative", () => {
    // The exact shape the trace caught: whisper hallucinates "Thank you." onto
    // a silent segment, it reaches userTranscripts, and checkGrounding then
    // happily confirms it was said — so the quote is real and the value is
    // fiction.
    assert.ok(!quoteSupportsChoice("burial", "Thank you."));
    assert.ok(!quoteSupportsChoice("burial", "Bye-bye."));
    assert.ok(!quoteSupportsChoice("female", "I don't want my daughter stuck with the bill."));
    assert.ok(!quoteSupportsChoice("burial", ""));
    assert.ok(!quoteSupportsChoice("", "Burial."));
  });

  it("accepts the prospect actually naming it", () => {
    assert.ok(quoteSupportsChoice("burial", "Burial."));
    assert.ok(quoteSupportsChoice("burial", "Burial, I think."));
    assert.ok(quoteSupportsChoice("cremation", "cremation please"));
    assert.ok(quoteSupportsChoice("female", "Female."));
    // Plural/singular and inflection must not be treated as a different answer.
    assert.ok(quoteSupportsChoice("burial", "burials mostly"));
  });
});

describe("transcription hallucinations are not answers", () => {
  it("treats the courtesies whisper emits on silence as non-answers", () => {
    // Straight from the traced call: 'Thank you.' and 'Bye-bye.' were the two
    // most common transcripts, on a call where the prospect was mostly silent.
    for (const said of ["Thank you.", "thanks", "Bye-bye.", "Goodbye", "take care"]) {
      const v = adjudicateTurn({
        transcript: said,
        confidence: 0.98,
        transcriptionFailed: false,
        expectation: "choice",
        confidenceMin: 0.6,
      });
      assert.equal(v.outcome, "not_heard", `must not count as an answer: ${said}`);
      assert.equal(v.notHeardReason, "non_answer");
    }
  });

  it("still takes a real answer that merely contains a courtesy", () => {
    // "Burial, thanks" is an answer. Only the WHOLE utterance being a courtesy
    // disqualifies it.
    const v = adjudicateTurn({
      transcript: "Burial, thanks.",
      confidence: 0.98,
      transcriptionFailed: false,
      expectation: "choice",
      confidenceMin: 0.6,
    });
    assert.equal(v.outcome, "heard_answer");
  });
});
