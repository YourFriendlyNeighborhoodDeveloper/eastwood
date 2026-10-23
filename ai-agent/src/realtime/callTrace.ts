/**
 * Per-turn call tracing: one structured record per conversational turn,
 * carrying every fact needed to reconstruct what the control loop did and why.
 *
 * The existing `turn latency` log answers "how long did each stage take". It
 * cannot answer "why did the call not advance", because it carries no words,
 * no slot, no verdict and no state. Reconstructing that from the interleaved
 * event log means correlating five message types by timestamp across a
 * 200-line dump — which is how a call that never left `greeting` for 208
 * seconds went unnoticed.
 *
 * A turn here is the full cycle: the assistant speaks, the prospect answers,
 * the turn is ruled on, a slot is (or is not) recorded, and the assistant
 * speaks again. Turn N's "next assistant response" is turn N+1's opening
 * utterance, so the record is emitted when the NEXT turn starts — one turn
 * behind live, and flushed at session end.
 *
 * Everything is derived from events the handler already receives. Nothing here
 * changes control flow: if tracing throws, the call must not notice.
 */

import { logger } from "../observability.js";
import type { QuestionExpectation, TurnVerdict } from "../discovery/turnGate.js";

/** One conversational turn, as it is built up across the events of that turn. */
export interface TurnTrace {
  seq: number;
  /** ms since the call started, for every timestamp below. */
  // 1 — the assistant began producing this turn's audio.
  assistantStartedMs: number | null;
  // 2 — response.done for that turn.
  assistantFinishedMs: number | null;
  /** What she actually said, as transcribed from her own output. */
  assistantText: string;
  // 3 — VAD detected the prospect's voice.
  userSpeechDetectedMs: number | null;
  // 4 — VAD closed the prospect's turn / transcription finalized.
  userSpeechEndedMs: number | null;
  userTranscriptFinalizedMs: number | null;
  // 5 — the prospect's words.
  transcript: string | null;
  // 7 — mean per-token recognition probability, null when unscored.
  transcriptConfidence: number | null;
  /** What the gate ruled, and what it was measuring the turn against. */
  verdict: TurnVerdict["outcome"] | null;
  verdictReason: string | null;
  expectation: QuestionExpectation | null;
  /** The question this turn was answering, as read from her own words. */
  pendingQuestion: string | null;
  // 10 — tool calls made during this turn, in order.
  tools: Array<{
    name: string;
    // 6 — the slot the tool tried to record.
    captureKey?: string;
    value?: unknown;
    heardVerbatim?: string;
    // the slot state the tool ruled: UNKNOWN | HEARD | CONFIRMED
    slotState?: string;
    captured?: boolean;
    // 7 — grounding confidence for the capture.
    confidence?: number | null;
    reason?: string;
    durationMs?: number;
  }>;
  // 8 / 9 — the sales machine before and after this turn.
  stateBefore: string | null;
  stateAfter: string | null;
  /** Quote progression, snapshotted at the end of the turn. */
  quoteReady: boolean | null;
  missingForQuote: string[] | null;
  // 11 — filled in from the next turn's opening utterance.
  nextAssistantResponse: string | null;
  /** True when the assistant produced a response that said nothing at all. */
  silentResponse: boolean;
  /** Set when the handler overrode the session prompt for this turn — a
   *  clarification. The recovery loop is exactly the run of these. */
  wasClarification: boolean;
}

function emptyTurn(seq: number): TurnTrace {
  return {
    seq,
    assistantStartedMs: null,
    assistantFinishedMs: null,
    assistantText: "",
    userSpeechDetectedMs: null,
    userSpeechEndedMs: null,
    userTranscriptFinalizedMs: null,
    transcript: null,
    transcriptConfidence: null,
    verdict: null,
    verdictReason: null,
    expectation: null,
    pendingQuestion: null,
    tools: [],
    stateBefore: null,
    stateAfter: null,
    quoteReady: null,
    missingForQuote: null,
    nextAssistantResponse: null,
    silentResponse: false,
    wasClarification: false,
  };
}

/**
 * Accumulates turns for one call. One instance per session, held on the
 * session and dropped with it.
 */
export class CallTrace {
  readonly sessionId: string;
  private readonly startedAt: number;
  private readonly turns: TurnTrace[] = [];
  private current: TurnTrace;
  private seq = 0;
  /** Whether the response currently generating has produced words / a tool. */
  private responseSpoke = false;
  private responseHadTool = false;
  /** Responses that produced nothing at all, across the whole call. */
  private silentResponseCount = 0;
  /** Tool calls awaiting their result, by token — see toolInvoked. */
  private readonly inFlightTools = new Map<number, TurnTrace["tools"][number]>();
  private toolToken = 0;

  constructor(sessionId: string, startedAt: number) {
    this.sessionId = sessionId;
    this.startedAt = startedAt;
    this.current = emptyTurn(this.seq);
  }

  private ms(at = Date.now()): number {
    return at - this.startedAt;
  }

  /**
   * The prospect started a turn of speech.
   *
   * A second speech_started with no assistant utterance in between means the
   * prospect spoke twice in a row — she did not reply to them. That is a
   * finding in itself, so it rolls the turn rather than merging the two.
   */
  userSpeechDetected(): void {
    if (this.current.userSpeechDetectedMs !== null) this.rollTurn();
    this.current.userSpeechDetectedMs = this.ms();
  }

  userSpeechEnded(): void {
    this.current.userSpeechEndedMs = this.ms();
  }

  transcriptFinalized(text: string, confidence: number | null): void {
    this.current.userTranscriptFinalizedMs = this.ms();
    this.current.transcript = text;
    this.current.transcriptConfidence = confidence;
  }

  gateRuled(
    outcome: TurnVerdict["outcome"],
    reason: string,
    expectation: QuestionExpectation,
    pendingQuestion: string | null
  ): void {
    this.current.verdict = outcome;
    this.current.verdictReason = reason;
    this.current.expectation = expectation;
    this.current.pendingQuestion = pendingQuestion;
  }

  responseRequested(isClarification: boolean): void {
    if (isClarification) this.current.wasClarification = true;
  }

  /**
   * The assistant began producing words.
   *
   * This is the turn boundary. A conversational turn is one assistant
   * utterance and the prospect's reply to it, so the moment she starts
   * speaking AGAIN — after a turn that already has both — a new turn begins.
   * Rolling on the prospect's speech instead put her question and their answer
   * to the PREVIOUS question in the same record, which reads as the loop
   * working when it is one behind.
   */
  assistantStarted(): void {
    if (this.current.assistantStartedMs !== null && this.current.transcript !== null) {
      this.rollTurn();
    }
    if (this.current.assistantStartedMs === null) {
      this.current.assistantStartedMs = this.ms();
    }
  }

  assistantSpoke(delta: string): void {
    this.assistantStarted();
    this.current.assistantText += delta;
    this.responseSpoke = true;
  }

  /** response.created — a new response is being generated for this turn. */
  responseStarted(): void {
    this.responseSpoke = false;
    this.responseHadTool = false;
  }

  /**
   * response.done. A response that produced neither words nor a tool call did
   * nothing at all — the model was asked to act and returned silence. That is
   * the "control loop advanced without waiting" signature: something requested
   * a response before there was anything for the model to say.
   */
  assistantFinished(): void {
    this.current.assistantFinishedMs = this.ms();
    if (!this.responseSpoke && !this.responseHadTool) {
      this.current.silentResponse = true;
      this.silentResponseCount += 1;
    }
    this.responseSpoke = false;
    this.responseHadTool = false;
  }

  /**
   * A tool call, attributed to the turn whose transcript justifies it.
   *
   * A turn boundary opens when she starts speaking again, but the capture for
   * the answer she just heard is made INSIDE that next utterance ("Right." →
   * update_lead_field). Left on the current turn it reads as recording an
   * answer nobody had given yet. So a tool arriving before this turn has a
   * transcript belongs to the previous turn — the same rule update_lead_field
   * enforces on itself, where a capture's evidence must post-date the last
   * speech boundary.
   */
  toolInvoked(name: string, input: Record<string, unknown>): number {
    this.responseHadTool = true;
    const target =
      this.current.transcript === null && this.turns.length > 0
        ? this.turns[this.turns.length - 1]!
        : this.current;
    const record: TurnTrace["tools"][number] = {
      name,
      ...(typeof input["captureKey"] === "string" ? { captureKey: input["captureKey"] } : {}),
      ...("value" in input ? { value: input["value"] } : {}),
      ...(typeof input["heardVerbatim"] === "string"
        ? { heardVerbatim: input["heardVerbatim"] }
        : {}),
    };
    target.tools.push(record);
    // A token, not an index: the tool may live on a turn that has already been
    // closed, so its position in `current.tools` means nothing by the time the
    // result comes back.
    const token = this.toolToken++;
    this.inFlightTools.set(token, record);
    return token;
  }

  toolReturned(token: number, result: Record<string, unknown>, durationMs: number): void {
    const t = this.inFlightTools.get(token);
    if (!t) return;
    this.inFlightTools.delete(token);
    t.durationMs = durationMs;
    if (typeof result["slotState"] === "string") t.slotState = result["slotState"];
    if (typeof result["captured"] === "boolean") t.captured = result["captured"];
    if (typeof result["confidence"] === "number") t.confidence = result["confidence"];
    if (typeof result["reason"] === "string") t.reason = result["reason"];
  }

  /**
   * Machine state and quote progression, sampled after each tool — and so
   * attributed to the same turn as that tool, for the same reason. Left on the
   * current turn, a capture and the state it produced land in different rows
   * and the trace reads as though the flow lagged a turn behind the answers.
   */
  noteState(state: string, quoteReady: boolean, missingForQuote: string[]): void {
    const target =
      this.current.transcript === null && this.turns.length > 0
        ? this.turns[this.turns.length - 1]!
        : this.current;
    if (target.stateBefore === null) target.stateBefore = state;
    target.stateAfter = state;
    target.quoteReady = quoteReady;
    target.missingForQuote = missingForQuote;
    // The next turn opens in whatever state this one left behind.
    if (target !== this.current) this.current.stateBefore = state;
  }

  /** Close the current turn and start a fresh one. */
  private rollTurn(): void {
    const done = this.current;
    this.turns.push(done);
    this.seq += 1;
    this.current = emptyTurn(this.seq);
    // Carry state forward so a turn with no tool call still reports where the
    // machine stood.
    this.current.stateBefore = done.stateAfter;
    // 11 — the previous turn's "next assistant response" is not known until
    // the assistant speaks again, so it is backfilled in emit().
  }

  /**
   * Emit every completed turn. Called once at session end: the whole point is
   * a timeline that can be read in order, and the "next assistant response"
   * field cannot be filled for turn N until turn N+1 has spoken.
   */
  flush(reason: string): TurnTrace[] {
    this.turns.push(this.current);
    for (let i = 0; i < this.turns.length - 1; i++) {
      this.turns[i]!.nextAssistantResponse = this.turns[i + 1]!.assistantText.trim() || null;
    }
    for (const t of this.turns) {
      logger.info(
        { sessionId: this.sessionId, trace: "turn", ...t },
        `turn trace #${t.seq}`
      );
    }
    logger.info(
      {
        sessionId: this.sessionId,
        trace: "call",
        reason,
        turns: this.turns.length,
        durationMs: this.ms(),
        // The four questions this trace exists to answer, pre-computed.
        silentResponses: this.silentResponseCount,
        clarifications: this.turns.filter((t) => t.wasClarification).length,
        longestClarificationRun: longestRun(this.turns, (t) => t.wasClarification),
        turnsWithNoTranscript: this.turns.filter((t) => t.transcript === null).length,
        capturesAttempted: this.turns.reduce(
          (n, t) => n + t.tools.filter((x) => x.name === "update_lead_field").length,
          0
        ),
        capturesConfirmed: this.turns.reduce(
          (n, t) => n + t.tools.filter((x) => x.slotState === "CONFIRMED").length,
          0
        ),
        quoteEverReady: this.turns.some((t) => t.quoteReady === true),
        finalState: this.turns.at(-1)?.stateAfter ?? null,
        finalMissingForQuote: lastNonNull(this.turns.map((t) => t.missingForQuote)),
      },
      "call trace summary"
    );
    return this.turns;
  }
}

function longestRun(turns: TurnTrace[], pred: (t: TurnTrace) => boolean): number {
  let best = 0;
  let run = 0;
  for (const t of turns) {
    run = pred(t) ? run + 1 : 0;
    if (run > best) best = run;
  }
  return best;
}

function lastNonNull<T>(xs: Array<T | null>): T | null {
  for (let i = xs.length - 1; i >= 0; i--) if (xs[i] !== null) return xs[i]!;
  return null;
}
