/**
 * The three-state lifecycle every required slot moves through.
 *
 *     UNKNOWN ──heard clearly, answers the question──> CONFIRMED
 *        │                                                 ▲
 *        └──heard, but not clearly enough──> HEARD ─────────┘
 *                                              (read back and agreed)
 *
 * UNKNOWN    Nothing usable. The default, and a perfectly acceptable place for
 *            a slot to stay for the whole call — a licensed agent can confirm
 *            it later, and a missing value is recoverable in a way a wrong one
 *            is not.
 *
 * HEARD      Words arrived and they trace to something the prospect really
 *            said, but the capture is not safe to trust yet: recognition
 *            confidence was below threshold, or the words do not have the
 *            shape of an answer to the question asked. A HEARD slot holds a
 *            CANDIDATE, not a value. It is never written to `captured`, never
 *            reaches the quote engine, and never reaches the handoff brief.
 *
 * CONFIRMED  The only state that stores anything. Requires all three of:
 *            finalized transcript, confidence above threshold, and words that
 *            semantically answer the question — or an explicit read-back that
 *            the prospect agreed to.
 *
 * The assistant may only advance to the next question once the slot it is
 * working on reaches CONFIRMED. That rule is enforced in the turn gate (before
 * she speaks) and here (before anything is stored); both are needed, because
 * refusing to store a value the prospect has already been thanked for fixes
 * the record while leaving the conversation broken.
 */

import type { GroundingResult } from "./transcriptGrounding.js";

export type SlotState = "UNKNOWN" | "HEARD" | "CONFIRMED";

export interface SlotRecord {
  captureKey: string;
  field: string;
  state: SlotState;
  /** Set only at CONFIRMED. This is what `captured` mirrors. */
  value: unknown;
  /** The value awaiting a read-back. Held at HEARD, never stored. */
  candidate: unknown;
  /** The prospect's own words the model reported hearing. */
  heardVerbatim: string;
  /** The transcript excerpt the words were traced to, when grounded. */
  matched: string | null;
  /** Mean per-token probability of the supporting transcript, or null. */
  confidence: number | null;
  /** How many times a capture has been attempted for this slot. */
  attempts: number;
  /** Why it is not CONFIRMED yet. Null once it is. */
  blockedReason: string | null;
  updatedAt: number;
}

export type CaptureVerdict = {
  next: SlotState;
  /** Why — logged, and fed back to the model as its next instruction. */
  reason: string;
  /** True when the value was stored without a verified transcript behind it
   *  (transcription down for the whole call, lenient mode). CONFIRMED, but
   *  flagged for the licensed agent rather than silently trusted. */
  unverified: boolean;
};

export interface CaptureJudgement {
  /** Result of checking the model's quote against the call transcripts. */
  grounding: GroundingResult;
  /** Confidence of the supporting transcript, or null when unscored. */
  confidence: number | null;
  confidenceMin: number;
  /** Whether the quoted words have the shape of an answer to what was asked.
   *  Pass true when there is no pending question to judge against. */
  answersQuestion: boolean;
  /** The model set confirmed=true, i.e. it read the value back out loud. */
  readBackConfirmed: boolean;
  /** State of this slot before this attempt. */
  priorState: SlotState;
  /** Transcription produced nothing at all on this call and the deployment
   *  accepts unverified values rather than stalling the call. */
  allowUnverifiable: boolean;
  /** The gate's ruling on the turn this capture is being made from. A capture
   *  claimed from a turn where nothing was heard is refused outright, whatever
   *  the model reports. */
  turnWasHeard: boolean;
}

/**
 * Decide what state a slot moves to on a capture attempt.
 *
 * Every path that is not CONFIRMED stores nothing. The distinction between
 * UNKNOWN and HEARD is what the assistant does next: UNKNOWN means ask again,
 * HEARD means read the value back and get a plain yes.
 */
export function judgeCapture(j: CaptureJudgement): CaptureVerdict {
  // The turn the model is claiming to have heard this in produced no usable
  // speech at all. Nothing said on it can support anything — including a quote
  // that happens to match an OLDER turn, which is how a question answered ten
  // minutes ago gets recycled as the answer to this one.
  if (!j.turnWasHeard) {
    return {
      next: "UNKNOWN",
      reason:
        "nothing usable was heard on this turn, so there is no answer to record",
      unverified: false,
    };
  }

  if (j.grounding.status === "rejected") {
    return { next: "UNKNOWN", reason: j.grounding.reason, unverified: false };
  }

  if (j.grounding.status === "unverifiable") {
    if (!j.allowUnverifiable) {
      return {
        next: "UNKNOWN",
        reason:
          "nothing said on this call has been transcribed, so no answer can be verified",
        unverified: false,
      };
    }
    // Transcription is down for the whole call. Refusing every capture would
    // leave the assistant re-asking the same question until the prospect hangs
    // up, so the value is kept and permanently flagged instead of trusted.
    return {
      next: "CONFIRMED",
      reason:
        "stored without transcript verification — input transcription produced " +
        "nothing on this call",
      unverified: true,
    };
  }

  // ── Grounded. Now: was it heard well enough, and does it answer? ──────────
  const lowConfidence =
    typeof j.confidence === "number" && j.confidence < j.confidenceMin;

  if (j.readBackConfirmed) {
    // A read-back confirms a value that was already HEARD. It cannot be used
    // to promote a slot straight from UNKNOWN — there was nothing to read
    // back — which is what stops confirmed=true being used to force a value
    // through on its first attempt.
    if (j.priorState === "HEARD") {
      return {
        next: "CONFIRMED",
        reason: "the prospect agreed to the value when it was read back",
        unverified: true,
      };
    }
    if (!lowConfidence && j.answersQuestion) {
      return {
        next: "CONFIRMED",
        reason: "heard clearly and it answers the question",
        unverified: false,
      };
    }
    return {
      next: "HEARD",
      reason:
        "confirmed=true was set on a value that had not been held for read-back " +
        "— read it back to the prospect first, then call this again",
      unverified: false,
    };
  }

  if (lowConfidence) {
    return {
      next: "HEARD",
      reason: `recognition confidence ${j.confidence?.toFixed(2)} is below the ${j.confidenceMin} threshold — read the value back before storing it`,
      unverified: false,
    };
  }

  if (!j.answersQuestion) {
    return {
      next: "HEARD",
      reason:
        "the words quoted do not answer the question that was asked — confirm " +
        "what they meant before storing it",
      unverified: false,
    };
  }

  return {
    next: "CONFIRMED",
    reason: "heard clearly and it answers the question",
    unverified: false,
  };
}

/** A fresh slot in its default state. Nothing is known until it is heard. */
export function emptySlot(captureKey: string, field: string): SlotRecord {
  return {
    captureKey,
    field,
    state: "UNKNOWN",
    value: undefined,
    candidate: undefined,
    heardVerbatim: "",
    matched: null,
    confidence: null,
    attempts: 0,
    blockedReason: null,
    updatedAt: 0,
  };
}

/** Current state of a slot, for callers that have not seen it before. */
export function slotState(
  slots: Record<string, SlotRecord> | undefined,
  captureKey: string
): SlotState {
  return slots?.[captureKey]?.state ?? "UNKNOWN";
}

/**
 * Slots that were heard but never confirmed. These are the questions the
 * licensed agent still has to ask: something was said, and we deliberately
 * refused to write down a guess at what it was.
 */
export function heardButUnconfirmed(
  slots: Record<string, SlotRecord> | undefined
): string[] {
  return Object.values(slots ?? {})
    .filter((s) => s.state === "HEARD")
    .map((s) => s.captureKey)
    .sort();
}
