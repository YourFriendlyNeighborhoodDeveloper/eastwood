/**
 * Slot integrity: a stored qualification field must reflect what the prospect
 * actually said.
 *
 * These cover the failure mode observed on live calls — the model receives a
 * clipped or noisy utterance, completes it into something plausible, and
 * advances as though it got a real answer. Every case below asserts the
 * capture is REFUSED rather than silently stored, because a wrong value is
 * worse than a missing one: nobody knows to go back and check it.
 */
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
// Pinned so these assertions describe a fixed threshold rather than tracking
// whatever the current default happens to be.
process.env["TRANSCRIPT_CONFIDENCE_MIN"] = "0.6";

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import {
  checkGrounding,
  confidenceFromLogprobs,
  normalize,
} from "../discovery/transcriptGrounding.js";
import { buildProspectProfile } from "../discovery/prospectProfile.js";
import type { LeadContext, SessionState } from "../types.js";

let toolHandlers: typeof import("../tools/toolHandlers.js").toolHandlers;

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

/** A session whose transcripts carry explicit confidence scores. */
function makeScoredSession(
  transcripts: Array<{ text: string; confidence: number | null }>
): SessionState {
  const s = makeSession([]);
  s.userTranscripts = transcripts.map((t) => ({
    text: t.text,
    at: Date.now(),
    confidence: t.confidence,
  }));
  return s;
}

function makeSession(transcripts: string[]): SessionState {
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
    userTranscripts: transcripts.map((text) => ({ text, at: Date.now() })),
    captureEvidence: {},
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

describe("transcript grounding", () => {
  it("accepts a value the prospect plainly said", () => {
    const r = checkGrounding("no, never smoked", [{ text: "No, never smoked." }]);
    assert.equal(r.status, "grounded");
  });

  it("accepts spoken-number forms against a digit value", () => {
    const r = checkGrounding("sixty eight", [{ text: "I'm sixty eight." }]);
    assert.equal(r.status, "grounded");
  });

  it("tolerates small wording differences from the transcriber", () => {
    const r = checkGrounding("burial costs mainly", [
      { text: "Mainly for the burial costs, yeah." },
    ]);
    assert.equal(r.status, "grounded");
  });

  it("REJECTS a value the prospect never said", () => {
    const r = checkGrounding("I take metformin for diabetes", [
      { text: "Sorry, could you repeat that?" },
    ]);
    assert.equal(r.status, "rejected");
  });

  it("REJECTS an empty quote — no evidence is not evidence", () => {
    const r = checkGrounding("", [{ text: "Yes that's right." }]);
    assert.equal(r.status, "rejected");
  });

  it("reports unverifiable (not grounded) when transcription has not landed yet", () => {
    // Transcription runs async and can lag a tool call. That is not proof of
    // invention — but it must never be reported as verified either.
    const r = checkGrounding("no tobacco", []);
    assert.equal(r.status, "unverifiable");
  });

  it("normalizes punctuation and case", () => {
    assert.equal(normalize("Yes, I do!"), "yes i do");
  });
});

describe("update_lead_field refuses ungrounded captures", () => {
  before(async () => {
    ({ toolHandlers } = await import("../tools/toolHandlers.js"));
  });

  it("stores a field the prospect actually answered", async () => {
    const session = makeSession(["No, I've never used tobacco."]);
    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "No, I've never used tobacco",
    });

    assert.equal(res["captured"], true);
    assert.equal(session.captured["tobacco"], "none");
    assert.equal(session.captureEvidence["tobacco"]?.grounding, "grounded");
  });

  it("refuses a hallucinated answer and tells the model to ask again", async () => {
    // The prospect's audio was noise; the model 'heard' a tidy answer anyway.
    const session = makeSession(["...", "[background noise]"]);
    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "no I don't smoke",
    });

    assert.equal(res["captured"], false);
    assert.equal(res["rejected"], true);
    assert.equal(session.captured["tobacco"], undefined, "nothing may be stored");
    assert.match(String(res["nextStep"]), /didn't catch that|say it again/i);
  });

  it("refuses when the model supplies no verbatim quote at all", async () => {
    const session = makeSession(["Yeah that sounds about right."]);
    const res = await capture(session, {
      field: "height_weight",
      captureKey: "height_weight",
      value: "5'10\" 190 lbs",
    });

    assert.equal(res["captured"], false);
    assert.equal(session.captured["height_weight"], undefined);
  });

  it("does not let an unanswered yes/no question become a 'no'", async () => {
    // Classic failure: question asked, prospect said something unrelated,
    // model records the absence as a negative answer.
    const session = makeSession(["Hang on, someone's at the door."]);
    const res = await capture(session, {
      field: "health_screening",
      captureKey: "conditions",
      value: "none",
      heardVerbatim: "no none of those",
    });

    assert.equal(res["captured"], false);
    assert.equal(session.captured["conditions"], undefined);
  });

  it("does not let overlapping speech produce a captured value", async () => {
    const session = makeSession(["— sorry, go ahead —"]);
    const res = await capture(session, {
      field: "coverage_target",
      captureKey: "desired_coverage_amount",
      value: 25000,
      heardVerbatim: "twenty five thousand",
    });

    assert.equal(res["captured"], false);
    assert.equal(session.captured["desired_coverage_amount"], undefined);
  });

  it("records each stored slot's provenance for audit", async () => {
    const session = makeSession(["My daughter Sarah — she's the one."]);
    await capture(session, {
      field: "beneficiary",
      captureKey: "beneficiary_name",
      value: "Sarah",
      heardVerbatim: "my daughter Sarah",
    });

    const ev = session.captureEvidence["beneficiary_name"]!;
    assert.equal(ev.grounding, "grounded");
    assert.match(ev.heardVerbatim, /Sarah/);
    assert.ok(ev.at > 0, "capture time is recorded");
  });

  it("keeps a slot UNKNOWN rather than guessing across several bad attempts", async () => {
    const session = makeSession(["What?", "Sorry?", "Say again?"]);
    for (const v of ["none", "no", "never"]) {
      await capture(session, {
        field: "tobacco",
        captureKey: "tobacco",
        value: v,
        heardVerbatim: v,
      });
    }
    assert.equal(
      session.captured["tobacco"],
      undefined,
      "repeated failures to hear must leave the slot unknown, never guessed"
    );
  });
});

describe("a call where transcription produced nothing", () => {
  before(async () => {
    ({ toolHandlers } = await import("../tools/toolHandlers.js"));
  });

  it("keeps the value but marks it unverified, so the call stays usable", async () => {
    // Transcription is down — nothing can be verified either way. Refusing
    // every capture would leave Imogen re-asking the same question until the
    // prospect hangs up, so the value is kept and flagged instead.
    const session = makeSession([]);
    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "never smoked",
    });

    assert.equal(res["captured"], true);
    assert.equal(session.captureEvidence["tobacco"]?.grounding, "unverifiable");
  });

  it("surfaces every unverified field to the agent who has to act on it", async () => {
    const session = makeSession([]);
    await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "never smoked",
    });

    const profile = buildProspectProfile(session, { appointmentTime: "" });
    assert.deepEqual(profile.unverified_fields, ["tobacco"]);
  });

  it("lists nothing when every value was heard clearly", async () => {
    const session = makeScoredSession([
      { text: "No, never smoked.", confidence: 0.97 },
    ]);
    await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "never smoked",
    });

    const profile = buildProspectProfile(session, { appointmentTime: "" });
    assert.deepEqual(profile.unverified_fields, []);
  });
});

describe("transcription confidence", () => {
  it("scores a clean transcription near certainty", () => {
    // logprob ~0 ⇒ probability ~1.
    const c = confidenceFromLogprobs([{ logprob: -0.01 }, { logprob: -0.02 }]);
    assert.ok(c !== null && c > 0.95, `expected high confidence, got ${c}`);
  });

  it("scores a mangled transcription low", () => {
    const c = confidenceFromLogprobs([{ logprob: -2.5 }, { logprob: -3.0 }]);
    assert.ok(c !== null && c < 0.3, `expected low confidence, got ${c}`);
  });

  it("does not let a long clear answer score worse than a short one", () => {
    // Guards the mean-vs-product choice: multiplying token probabilities
    // decays toward zero with length, which would flag every long sentence.
    const short = confidenceFromLogprobs([{ logprob: -0.1 }, { logprob: -0.1 }])!;
    const long = confidenceFromLogprobs(Array(40).fill({ logprob: -0.1 }))!;
    assert.ok(Math.abs(short - long) < 0.01, `length changed the score: ${short} vs ${long}`);
  });

  it("reports null — never a passing score — when the API sends no logprobs", () => {
    // A missing measurement must not read as a confident one.
    assert.equal(confidenceFromLogprobs(undefined), null);
    assert.equal(confidenceFromLogprobs([]), null);
    assert.equal(confidenceFromLogprobs([{ token: "hi" }]), null);
  });

  it("carries the matched transcript's confidence through grounding", () => {
    const r = checkGrounding("never smoked", [
      { text: "No, never smoked.", confidence: 0.42 },
    ]);
    assert.equal(r.status, "grounded");
    assert.equal(r.status === "grounded" ? r.confidence : undefined, 0.42);
  });

  it("scores against the most recent supporting turn, not the earliest", () => {
    // The prospect is answering NOW; an identical phrase from ten minutes ago
    // is not the evidence for this capture, and its score is not this score.
    const r = checkGrounding("yes that's right", [
      { text: "Yes that's right.", confidence: 0.99 },
      { text: "Yes that's right.", confidence: 0.31 },
    ]);
    assert.equal(r.status === "grounded" ? r.confidence : undefined, 0.31);
  });
});

describe("low-confidence captures are confirmed, not stored", () => {
  before(async () => {
    ({ toolHandlers } = await import("../tools/toolHandlers.js"));
  });

  it("holds a poorly-heard value for read-back instead of recording it", async () => {
    const session = makeScoredSession([
      { text: "I'm sixty eight.", confidence: 0.3 },
    ]);
    const res = await capture(session, {
      field: "age",
      captureKey: "age",
      value: 68,
      heardVerbatim: "I'm sixty eight",
    });

    assert.equal(res["captured"], false);
    assert.equal(res["needsConfirmation"], true);
    assert.equal(session.captured["age"], undefined, "nothing may be stored yet");
    assert.match(String(res["nextStep"]), /is that correct|read the value back/i);
  });

  it("stores it once the prospect has confirmed the read-back", async () => {
    const session = makeScoredSession([
      { text: "I'm sixty eight.", confidence: 0.3 },
    ]);
    const args = {
      field: "age",
      captureKey: "age",
      value: 68,
      heardVerbatim: "I'm sixty eight",
    };
    await capture(session, args);
    const res = await capture(session, { ...args, confirmed: true });

    assert.equal(res["captured"], true);
    assert.equal(session.captured["age"], 68);
    assert.equal(session.captureEvidence["age"]?.confidence, 0.3);
  });

  it("stores a confidently-heard value without any read-back", async () => {
    const session = makeScoredSession([
      { text: "No, never smoked.", confidence: 0.97 },
    ]);
    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "never smoked",
    });

    assert.equal(res["captured"], true);
    assert.equal(res["needsConfirmation"], undefined, "a clear answer must not stall");
  });

  it("does not let confirmed=true launder a value the prospect never said", async () => {
    // The confirmation path forgives a poor score, never absent evidence.
    const session = makeScoredSession([{ text: "Who is this?", confidence: 0.99 }]);
    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "no I don't smoke",
      confirmed: true,
    });

    assert.equal(res["rejected"], true);
    assert.equal(session.captured["tobacco"], undefined);
  });

  it("records the transcript excerpt each stored value was traced to", async () => {
    const session = makeScoredSession([
      { text: "Mainly for the burial costs, yeah.", confidence: 0.88 },
    ]);
    await capture(session, {
      field: "purpose",
      captureKey: "purpose",
      value: "burial",
      heardVerbatim: "burial costs mainly",
    });

    const ev = session.captureEvidence["purpose"]!;
    assert.match(String(ev.matched), /burial costs/i);
    assert.equal(ev.confidence, 0.88);
    assert.equal(ev.grounding, "grounded");
  });
});
