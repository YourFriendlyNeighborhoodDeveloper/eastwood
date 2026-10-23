/**
 * UNKNOWN → HEARD → CONFIRMED, and the things that must never complete it.
 *
 * The live failure this covers: Imogen asks "what's your first name?", the
 * prospect says nothing usable, and she immediately answers "Perfect, thanks."
 * and asks the next question. The system had treated noise, silence, and an
 * un-finalized transcript as a completed slot.
 *
 * Every case below asserts the slot does NOT reach CONFIRMED and nothing is
 * stored. A slot that stays UNKNOWN costs one repeated question; a slot that
 * gets filled from noise is a permanent wrong value on an insurance record
 * that nobody knows to go back and check.
 */
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["TRANSCRIPT_CONFIDENCE_MIN"] = "0.6";

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import {
  adjudicateTurn,
  answersExpectation,
  classifyQuestion,
  isBackchannelOnly,
  isClipped,
  isFillerOnly,
  isNoiseOnly,
  isNonAnswerUtterance,
  type QuestionExpectation,
  type TurnEvidence,
} from "../discovery/turnGate.js";
import { judgeCapture, slotState } from "../discovery/slotState.js";
import type { LeadContext, SessionState } from "../types.js";

let toolHandlers: typeof import("../tools/toolHandlers.js").toolHandlers;

const CONFIDENCE_MIN = 0.6;

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

function makeSession(
  transcripts: Array<{ text: string; confidence?: number | null }> = []
): SessionState {
  return {
    lead: makeLead(),
    quote: null,
    quoteOptions: null,
    selectedQuoteOption: null,
    affordabilityConfirmed: null,
    quoteFailureReason: null,
    offeredSlots: [],
    calendly: null,
    appointmentBooked: false,
    captured: {},
    objections: [],
    appointment: null,
    dncCaptured: false,
    callEndedPublished: false,
    userTranscripts: transcripts.map((t) => ({
      text: t.text,
      at: Date.now(),
      confidence: t.confidence ?? null,
    })),
    captureEvidence: {},
    slots: {},
    turnVerdict: null,
    pendingQuestion: null,
  };
}

const noopBridge = { publishEvent: async () => {} } as any;
const noopActor = { send: () => {} } as any;

async function capture(
  session: SessionState,
  args: Record<string, unknown>
): Promise<Record<string, unknown>> {
  return toolHandlers["update_lead_field"]!(args, {
    lead: session.lead,
    sessionId: "sess-1",
    bridge: noopBridge,
    actor: noopActor,
    session,
  });
}

function evidence(over: Partial<TurnEvidence> = {}): TurnEvidence {
  return {
    transcript: null,
    confidence: null,
    transcriptionFailed: false,
    expectation: "yes_no",
    confidenceMin: CONFIDENCE_MIN,
    ...over,
  };
}

// ── The gate: what is and is not an answer ──────────────────────────────────

describe("a turn only counts as answered when something was actually said", () => {
  it("silence — no transcript ever finalized — does not answer", () => {
    const v = adjudicateTurn(evidence({ transcript: null }));
    assert.equal(v.outcome, "not_heard");
    assert.equal(v.notHeardReason, "no_transcript");
  });

  it("an empty transcript does not answer", () => {
    const v = adjudicateTurn(evidence({ transcript: "" }));
    assert.equal(v.outcome, "not_heard");
    assert.equal(v.notHeardReason, "empty");
  });

  it("a whitespace-only transcript does not answer", () => {
    assert.equal(adjudicateTurn(evidence({ transcript: "   \n " })).outcome, "not_heard");
  });

  it("background noise does not answer", () => {
    for (const noise of ["[background noise]", "(coughing)", "[BLANK_AUDIO]", "*static*", "..."]) {
      const v = adjudicateTurn(evidence({ transcript: noise }));
      assert.equal(v.outcome, "not_heard", `noise leaked through: ${noise}`);
    }
  });

  it("a failed transcription does not answer", () => {
    const v = adjudicateTurn(evidence({ transcriptionFailed: true, transcript: "yes" }));
    assert.equal(v.outcome, "not_heard");
    assert.equal(v.notHeardReason, "transcription_failed");
  });

  it("hesitation does not answer", () => {
    for (const t of ["uh...", "um", "uh, um", "hmm"]) {
      assert.equal(
        adjudicateTurn(evidence({ transcript: t })).outcome,
        "not_heard",
        `hesitation leaked through: ${t}`
      );
    }
  });

  it("an ambiguous grunt does not answer a yes/no question", () => {
    // "mhm" is a yes on a clean line and a passing car on a phone line. On an
    // insurance record the two are not interchangeable, and the recovery costs
    // one sentence: "was that a yes or a no?".
    const v = adjudicateTurn(evidence({ transcript: "mhm" }));
    assert.equal(v.outcome, "not_heard");
    assert.equal(v.notHeardReason, "backchannel_only");
  });

  it("a clipped fragment does not answer", () => {
    for (const t of ["twenty five thou-", "— sorry, go ahead —", "well I..."]) {
      assert.equal(
        adjudicateTurn(evidence({ transcript: t, expectation: "free_text" })).outcome,
        "not_heard",
        `fragment leaked through: ${t}`
      );
    }
  });

  it("them asking US something does not answer", () => {
    for (const t of ["Sorry?", "What was that?", "Who is this?", "Hang on", "Can you hear me?"]) {
      assert.equal(
        adjudicateTurn(evidence({ transcript: t })).outcome,
        "not_heard",
        `non-answer leaked through: ${t}`
      );
    }
  });

  it("a low-confidence transcript does not reach a confirmed answer", () => {
    // Distinct from not_heard: there IS a candidate, so the recovery is a
    // read-back rather than making them repeat the whole thing. What it must
    // never be is heard_answer.
    const v = adjudicateTurn(evidence({ transcript: "I'm sixty eight", confidence: 0.3, expectation: "number" }));
    assert.equal(v.outcome, "heard_unclear");
    assert.notEqual(v.outcome, "heard_answer");
  });

  it("accepts a plain yes and a plain no", () => {
    for (const t of ["yes", "no", "yeah", "nope"]) {
      assert.equal(
        adjudicateTurn(evidence({ transcript: t, confidence: 0.95 })).outcome,
        "heard_answer",
        `a real answer was rejected: ${t}`
      );
    }
  });

  it("accepts a qualified answer — it is still an answer", () => {
    // "I quit" and "I smoke occasionally" are complete answers. Sending these
    // back for a repeat would make the fix worse than the bug.
    for (const t of ["I quit", "I smoke occasionally", "used to, years ago", "only socially"]) {
      assert.equal(
        adjudicateTurn(evidence({ transcript: t, confidence: 0.9 })).outcome,
        "heard_answer",
        `a valid qualified answer was rejected: ${t}`
      );
    }
  });

  it("treats an unscored transcript as unscored, not as low-confidence", () => {
    // whisper-family models return no logprobs at all. Reading a missing score
    // as a failing one would gate every call made with one.
    const v = adjudicateTurn(evidence({ transcript: "no, never smoked", confidence: null }));
    assert.equal(v.outcome, "heard_answer");
  });

  it("separates 'said something else' from 'said nothing'", () => {
    // They spoke clearly, just not about what was asked. Making them repeat
    // would be the wrong recovery — she should deal with what they said.
    const v = adjudicateTurn(
      evidence({ transcript: "someone's at the door", confidence: 0.95, expectation: "yes_no" })
    );
    assert.equal(v.outcome, "heard_other");
  });
});

describe("non-answer detectors", () => {
  it("spots sound annotations without mistaking real speech for them", () => {
    assert.equal(isNoiseOnly("[background noise]"), true);
    assert.equal(isNoiseOnly("..."), true);
    assert.equal(isNoiseOnly("no, never smoked"), false);
  });

  it("spots filler without mistaking a hedged answer for it", () => {
    assert.equal(isFillerOnly("uh, um"), true);
    assert.equal(isFillerOnly("uh, no I don't"), false);
  });

  it("spots a bare backchannel without mistaking a real yes for it", () => {
    assert.equal(isBackchannelOnly("mhm"), true);
    assert.equal(isBackchannelOnly("uh huh"), true);
    assert.equal(isBackchannelOnly("yes"), false);
  });

  it("spots clipped and overlapped speech", () => {
    assert.equal(isClipped("twenty five thou-"), true);
    assert.equal(isClipped("— go ahead —"), true);
    // A complete sentence that merely trails off is a real answer.
    assert.equal(isClipped("No, I quit smoking years ago..."), false);
  });

  it("spots a bare non-answer but not one with a real answer attached", () => {
    assert.equal(isNonAnswerUtterance("sorry?"), true);
    assert.equal(isNonAnswerUtterance("who is this"), true);
    assert.equal(isNonAnswerUtterance("sorry, no"), false, "'sorry, no' is a no");
  });
});

describe("reading what the assistant asked", () => {
  const cases: Array<[string, QuestionExpectation]> = [
    ["Are you a smoker?", "yes_no"],
    ["Any tobacco or nicotine in the past twelve months?", "yes_no"],
    ["Do you currently have anything in place?", "yes_no"],
    ["What's your first name?", "name"],
    ["How much coverage were you thinking?", "amount"],
    ["Were you trying to stay around a certain monthly payment?", "amount"],
    ["How old are you?", "number"],
    ["What's got you looking into this?", "free_text"],
    ["Let me pull those numbers up for you.", "none"],
  ];

  for (const [question, expected] of cases) {
    it(`reads "${question}" as ${expected}`, () => {
      assert.equal(classifyQuestion(question), expected);
    });
  }

  it("uses the LAST question when she asked more than one thing", () => {
    assert.equal(
      classifyQuestion("That helps. And are you a smoker?"),
      "yes_no"
    );
  });

  it("scores a qualified answer as answering a yes/no question", () => {
    assert.equal(answersExpectation("I quit", "yes_no"), true);
    assert.equal(answersExpectation("I smoke occasionally", "yes_no"), true);
    assert.equal(answersExpectation("someone is at the door", "yes_no"), false);
  });
});

// ── The lifecycle ───────────────────────────────────────────────────────────

describe("the slot lifecycle refuses to skip states", () => {
  const grounded = { status: "grounded", matched: "I'm sixty eight", confidence: 0.9 } as const;

  function judge(over: Partial<Parameters<typeof judgeCapture>[0]> = {}) {
    return judgeCapture({
      grounding: grounded,
      confidence: 0.9,
      confidenceMin: CONFIDENCE_MIN,
      answersQuestion: true,
      readBackConfirmed: false,
      priorState: "UNKNOWN",
      allowUnverifiable: true,
      turnWasHeard: true,
      ...over,
    });
  }

  it("confirms a clear answer that answers the question", () => {
    assert.equal(judge().next, "CONFIRMED");
  });

  it("holds a poorly-heard answer at HEARD", () => {
    assert.equal(judge({ confidence: 0.3 }).next, "HEARD");
  });

  it("holds an answer that does not answer the question at HEARD", () => {
    assert.equal(judge({ answersQuestion: false }).next, "HEARD");
  });

  it("keeps a slot UNKNOWN when the turn produced nothing usable", () => {
    // Even a perfectly grounded quote cannot come from a turn where nothing
    // was said — that quote is matching an OLDER turn.
    assert.equal(judge({ turnWasHeard: false }).next, "UNKNOWN");
  });

  it("promotes HEARD to CONFIRMED once the prospect agrees to the read-back", () => {
    const v = judge({ confidence: 0.3, readBackConfirmed: true, priorState: "HEARD" });
    assert.equal(v.next, "CONFIRMED");
    assert.equal(v.unverified, true, "a read-back confirmation is flagged for the agent");
  });

  it("will not let confirmed=true jump a slot straight from UNKNOWN", () => {
    // Nothing was ever read back, so there was nothing for them to agree to.
    const v = judge({ confidence: 0.3, readBackConfirmed: true, priorState: "UNKNOWN" });
    assert.equal(v.next, "HEARD");
  });

  it("refuses outright when the quote is not in any transcript", () => {
    const v = judge({
      grounding: { status: "rejected", reason: "not said" },
      readBackConfirmed: true,
    });
    assert.equal(v.next, "UNKNOWN");
  });
});

// ── End to end through the tool ─────────────────────────────────────────────

describe("update_lead_field never fills a slot from a turn with no answer", () => {
  before(async () => {
    ({ toolHandlers } = await import("../tools/toolHandlers.js"));
  });

  /** A session whose most recent turn the gate ruled produced no answer. */
  function silentTurn(transcripts: Array<{ text: string; confidence?: number }> = []): SessionState {
    const s = makeSession(transcripts);
    s.lastSpeechEndedAt = Date.now();
    s.turnVerdict = {
      outcome: "not_heard",
      reason: "the turn contained sound but no speech",
      expectation: "name",
      at: Date.now() + 1,
    };
    return s;
  }

  it("refuses a capture claimed from a turn where nothing was heard", async () => {
    const res = await capture(silentTurn(), {
      field: "first_name",
      captureKey: "first_name",
      value: "Dolores",
      heardVerbatim: "Dolores",
    });
    assert.equal(res["captured"], false);
    assert.equal(res["slotState"], "UNKNOWN");
  });

  it("refuses even when the words match something said EARLIER in the call", async () => {
    // The specific leak this closes: the model re-quotes an answer from an
    // earlier question, it grounds cleanly against that old transcript, and a
    // slot gets filled from a turn in which the prospect said nothing at all.
    const session = silentTurn([{ text: "My name is Dolores.", confidence: 0.98 }]);
    const res = await capture(session, {
      field: "first_name",
      captureKey: "first_name",
      value: "Dolores",
      heardVerbatim: "My name is Dolores",
    });

    assert.equal(res["captured"], false);
    assert.equal(session.captured["first_name"], undefined, "nothing may be stored");
    assert.equal(slotState(session.slots, "first_name"), "UNKNOWN");
  });

  it("tells the assistant to ask again, and not to thank them", async () => {
    const res = await capture(silentTurn(), {
      field: "first_name",
      captureKey: "first_name",
      value: "Dolores",
      heardVerbatim: "Dolores",
    });
    const nextStep = String(res["nextStep"]);
    assert.match(nextStep, /didn't catch that|say it again/i);
    assert.match(nextStep, /do NOT thank them/i);
    assert.match(nextStep, /do NOT move to the next question/i);
  });

  it("still records normally on a turn that WAS heard", async () => {
    const session = makeSession([{ text: "It's Dolores.", confidence: 0.97 }]);
    session.lastSpeechEndedAt = Date.now();
    session.turnVerdict = {
      outcome: "heard_answer",
      reason: "answered",
      expectation: "name",
      at: Date.now() + 1,
    };

    const res = await capture(session, {
      field: "first_name",
      captureKey: "first_name",
      value: "Dolores",
      heardVerbatim: "It's Dolores",
    });

    assert.equal(res["captured"], true);
    assert.equal(res["slotState"], "CONFIRMED");
    assert.equal(slotState(session.slots, "first_name"), "CONFIRMED");
  });

  it("holds a yes/no slot at HEARD when the words are not a yes or a no", async () => {
    // They said something real, and it grounds — but it is not an answer to
    // the question asked, so it may not be recorded as one.
    const session = makeSession([
      { text: "Well, my wife keeps on at me about it.", confidence: 0.95 },
    ]);
    session.pendingQuestion = {
      text: "Are you a smoker?",
      expectation: "yes_no",
      at: Date.now(),
    };

    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "my wife keeps on at me about it",
    });

    assert.equal(res["captured"], false);
    assert.equal(res["slotState"], "HEARD");
    assert.equal(session.captured["tobacco"], undefined);
    assert.match(String(res["nextStep"]), /yes or a no/i);
  });

  it("accepts a qualified answer to a yes/no question without a read-back", async () => {
    const session = makeSession([{ text: "I quit, about five years ago.", confidence: 0.95 }]);
    session.pendingQuestion = {
      text: "Are you a smoker?",
      expectation: "yes_no",
      at: Date.now(),
    };

    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "former",
      heardVerbatim: "I quit, about five years ago",
    });

    assert.equal(res["captured"], true, "a real answer must not be sent back for a repeat");
    assert.equal(session.captured["tobacco"], "former");
  });

  it("keeps the candidate off the record while a slot sits at HEARD", async () => {
    const session = makeSession([{ text: "I'm sixty eight.", confidence: 0.3 }]);
    await capture(session, {
      field: "age",
      captureKey: "age",
      value: 68,
      heardVerbatim: "I'm sixty eight",
    });

    assert.equal(slotState(session.slots, "age"), "HEARD");
    assert.equal(session.captured["age"], undefined, "a HEARD slot stores nothing");
    assert.equal(session.slots?.["age"]?.candidate, 68, "the candidate is held, not stored");
    assert.equal(session.captureEvidence["age"], undefined, "and it is not on the record");
  });

  it("walks UNKNOWN → HEARD → CONFIRMED and only then stores", async () => {
    const session = makeSession([{ text: "I'm sixty eight.", confidence: 0.3 }]);
    const args = { field: "age", captureKey: "age", value: 68, heardVerbatim: "I'm sixty eight" };

    assert.equal(slotState(session.slots, "age"), "UNKNOWN");
    await capture(session, args);
    assert.equal(slotState(session.slots, "age"), "HEARD");
    await capture(session, { ...args, confirmed: true });
    assert.equal(slotState(session.slots, "age"), "CONFIRMED");
    assert.equal(session.captured["age"], 68);
  });
});
