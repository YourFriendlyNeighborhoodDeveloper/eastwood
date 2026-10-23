import { createActor, type Actor } from "xstate";
import WebSocket from "ws";
import { loadConfig } from "../config.js";
import {
  logger,
  realtimeReconnects,
  sessionsActive,
  sessionsTotal,
  sessionsCreated,
  sessionsClosed,
  sessionsReaped,
  duplicateTerminalEvents,
  sessionDurationSec,
  turnLatencyMs,
  turnsTotal,
  watchdogStalls,
  silenceToFirstAudioMs,
  turnTotalMs,
  silentToolWaitMs,
  bargeIns,
  transcriptConfidence,
  turnGateVerdicts,
  turnGateWaitMs,
  acknowledgementsBlocked,
  questionsSkipped,
  turnGateEscalations,
} from "../observability.js";
import { PolicyEngine } from "../policy/policyEngine.js";
import { buildSystemPrompt } from "../prompts/buildSystemPrompt.js";
import { salesMachine } from "../machine/salesMachine.js";
import { filterToolsByAllowed } from "../tools/toolDefinitions.js";
import { deriveAllowedTools } from "../tools/toolGating.js";
import { handoffProfile, toolHandlers } from "../tools/toolHandlers.js";
import { missingForQuote, quoteUnlocked } from "../discovery/prospectProfile.js";
import {
  currentStepId,
  discoveryStateBlock,
  type DiscoveryStepId,
} from "../discovery/discoveryFlow.js";
import {
  confidenceFromLogprobs,
  type TranscriptLogprob,
} from "../discovery/transcriptGrounding.js";
import {
  adjudicateTurn,
  classifyQuestion,
  notHeardInstructions,
  skipQuestionInstructions,
  escalateInstructions,
  unclearInstructions,
  type QuestionExpectation,
  type TurnVerdict,
} from "../discovery/turnGate.js";
import { heardButUnconfirmed, type SlotRecord } from "../discovery/slotState.js";
import { CallTrace } from "./callTrace.js";
import type { CalendlyState, LeadContext, Quote, QuoteOption } from "../types.js";
import type { KafkaBridge } from "../bridge/kafkaBridge.js";

/**
 * The opening of an acknowledgement. Matched against the first words of an
 * assistant turn that the gate ruled produced no answer — "Perfect, thanks."
 * is the exact utterance this whole change exists to prevent, so it is caught
 * as it is being spoken as well as prevented before it starts.
 */
const ACKNOWLEDGEMENT_OPENING =
  /^\s*(?:and\s+|so\s+|okay,?\s+|ok,?\s+|alright,?\s+)?(perfect|great|brilliant|excellent|wonderful|lovely|fantastic|got it|gotcha|understood|noted|thanks|thank you|that helps|good to know|makes sense|no problem|right then)\b/i;

const policy = new PolicyEngine();

/**
 * Per-turn latency breakdown, from the moment the prospect stops talking to
 * the moment the assistant finishes responding. The Realtime API is a single
 * audio-native model — there is no separate STT→LLM→TTS pipeline to time, so
 * "first token" and "TTS start" collapse into one real event (the first audio
 * delta). Fields that have no real signal (e.g. when audio physically starts
 * playing on the prospect's handset) are left out rather than faked.
 */
interface TurnTiming {
  speechStoppedAt: number;
  /** input_audio_buffer.speech_stopped for the NEXT turn, i.e. when this
   *  turn's listening window actually closed. Distinct from speechStoppedAt,
   *  which is when the turn began. */
  speechEndedAt: number | null;
  /** input_audio_buffer.speech_started — VAD detected the prospect's voice.
   *  On the prospect's own turn this is when they began talking; while the
   *  assistant is speaking it is also the barge-in trigger. */
  speechStartedAt: number | null;
  /** input_audio_buffer.speech_started — prospect began talking over us. */
  bargeInAt: number | null;
  /** When Twilio's queued audio was flushed in response to the barge-in. */
  audioFlushedAt: number | null;
  /** First conversation.item.input_audio_transcription.delta — the earliest
   *  point any of the prospect's words had been recognized. Separates "the
   *  transcriber is slow to start" from "the transcriber is slow to finish". */
  firstTranscriptPartialAt: number | null;
  /** conversation.item.input_audio_transcription.completed, when enabled. */
  transcriptCompletedAt: number | null;
  /** response.created — the model has begun generating this turn. */
  responseCreatedAt: number | null;
  /** First response.audio_transcript.delta — first token, text-level. */
  firstTranscriptDeltaAt: number | null;
  /** First response.output_audio.delta actually forwarded to Twilio — the
   *  real "did sound start reaching the prospect" proxy. */
  firstAudioPacketSentAt: number | null;
  /** response.function_call_arguments.done. */
  functionCallDetectedAt: number | null;
  toolName: string | null;
  toolExecStartAt: number | null;
  toolExecEndAt: number | null;
  /** response.done — generation finished. */
  responseDoneAt: number | null;
}

function newTurnTiming(): TurnTiming {
  return {
    speechStoppedAt: Date.now(),
    speechEndedAt: null,
    speechStartedAt: null,
    bargeInAt: null,
    audioFlushedAt: null,
    firstTranscriptPartialAt: null,
    transcriptCompletedAt: null,
    responseCreatedAt: null,
    firstTranscriptDeltaAt: null,
    firstAudioPacketSentAt: null,
    functionCallDetectedAt: null,
    toolName: null,
    toolExecStartAt: null,
    toolExecEndAt: null,
    responseDoneAt: null,
  };
}

interface ActiveSession {
  sessionId: string;
  lead: LeadContext;
  actor: Actor<typeof salesMachine>;
  /** Set by the get_quote tool. Drives which closing tools are exposed. */
  quote: Quote | null;
  /** The three engine-priced options (C/B/A) once get_quote succeeds. */
  quoteOptions: QuoteOption[] | null;
  selectedQuoteOption: string | null;
  affordabilityConfirmed: boolean | null;
  /** Set when get_quote failed, unlocking an honest manual-quote booking. */
  quoteFailureReason: string | null;
  /** Real availability retrieved before any time was offered. */
  offeredSlots: string[];
  calendly: CalendlyState | null;
  appointmentBooked: boolean;
  /** Captured field values (captureKey → value), source of the handoff profile. */
  captured: Record<string, unknown>;
  objections: Array<{ objectionType: string; text: string }>;
  appointment: { appointmentId: string; date: string; time: string } | null;
  dncCaptured: boolean;
  callEndedPublished: boolean;
  /** True once Twilio bound the media stream — i.e. the call reached a person. */
  telephonyBound: boolean;
  /** True once the OpenAI Realtime session has been created/updated. */
  realtimeReady: boolean;
  /** True once the outbound opening has been sent — exactly one per call. */
  openingSent: boolean;
  /** Set synchronously at the start of reclamation so a second terminal event
   *  cannot double-publish, double-decrement, or re-tear-down. */
  closing: boolean;
  openaiWs: WebSocket | null;
  telephonyWs: WebSocket | null;
  streamSid: string | null;
  startedAt: number;
  /** Set the moment we expect the model to speak next (prospect just finished
   *  talking, or we explicitly requested a response). Null once the model has
   *  started or finished responding. The watchdog's whole state lives here. */
  awaitingResponseSince: number | null;
  /** Un-recovered stalls in a row on the current turn. Reset to 0 the instant
   *  any response.* event arrives. */
  consecutiveStalls: number;
  watchdogTimer: ReturnType<typeof setTimeout> | null;
  /** The in-flight per-turn latency breakdown, or null between turns. */
  turnTiming: TurnTiming | null;
  /** True while assistant audio is in flight — set on the first audio delta,
   *  cleared on response.done/cancel. Gates interruption handling so we only
   *  cancel and flush when there is actually something playing. */
  assistantSpeaking: boolean;
  /** Every finalized transcript of the PROSPECT's own speech this call. The
   *  sole evidence base for slot capture: a value that cannot be traced to
   *  something here was never actually said. */
  userTranscripts: Array<{ text: string; at: number; confidence?: number | null }>;
  /** Provenance for each stored slot — the prospect's own words and whether
   *  they were verified against a transcript. */
  captureEvidence: Record<
    string,
    {
      heardVerbatim: string;
      at: number;
      grounding: "grounded" | "unverifiable";
      confidence?: number | null;
      matched?: string;
    }
  >;
  /** When the prospect's most recent turn of speech ended — the boundary a
   *  capture's evidence must post-date. */
  lastSpeechEndedAt: number | null;
  /** True once any transcript has landed, i.e. transcription is working. */
  transcriptionEverSeen: boolean;
  /** Callbacks parked by waitForTranscriptSince, woken by the next transcript. */
  transcriptWaiters: Array<() => void>;
  /** Cleared if the API rejects the logprobs `include`, so the retry omits it. */
  requestTranscriptLogprobs: boolean;
  /** Bound in createSession; lets a tool handler wait for this turn's words. */
  waitForTranscriptSince: (sinceMs: number, timeoutMs: number) => Promise<void>;

  // ── Turn gate ─────────────────────────────────────────────────────────────
  /** Every required slot's position in UNKNOWN → HEARD → CONFIRMED. */
  slots: Record<string, SlotRecord>;
  /** Per-turn trace for this call — see callTrace.ts. Purely observational. */
  trace: CallTrace;
  /** The gate's ruling on the prospect's most recent turn. */
  turnVerdict: {
    outcome: TurnVerdict["outcome"];
    reason: string;
    expectation: QuestionExpectation;
    at: number;
  } | null;
  /** The assistant's last spoken question and the answer shape it implies. */
  pendingQuestion: { text: string; expectation: QuestionExpectation; at: number } | null;
  /** Monotonic id of the open gate. Guards a late transcript or a stale timer
   *  from ruling on a turn that has already been superseded. */
  gateSeq: number;
  /** Non-null while a gate is open, i.e. we are deciding whether the prospect
   *  answered and the model has NOT been asked to speak yet. */
  gateOpen: {
    seq: number;
    openedAt: number;
    transcript: string | null;
    confidence: number | null;
    transcriptionFailed: boolean;
    timer: ReturnType<typeof setTimeout> | null;
  } | null;
  /** Consecutive turns ruled not_heard. Drives escalation from "sorry, I
   *  didn't catch that" to naming the line problem. */
  /** Unusable turns in a row on the CURRENT question. Reset when a question is
   *  answered, and when one is abandoned — each question gets its own budget. */
  consecutiveNotHeard: number;
  /** Questions abandoned after using their whole recovery budget. Drives
   *  escalation, and tells the licensed agent what was never established. */
  skippedQuestions: Array<{
    question: string | null;
    step: DiscoveryStepId | null;
    reason: string;
    at: number;
  }>;
  /** Discovery steps closed by the recovery budget. The flow is otherwise
   *  derived from captured slots, so this is the only thing it has to carry:
   *  a step nobody could answer is not captured and must not be re-asked. */
  skippedSteps: Set<DiscoveryStepId>;
  /** True between our response.create and its response.done. Exactly one
   *  response may be in flight — this is what stops a second question being
   *  queued behind the first. */
  responseInFlight: boolean;
  /** A response we wanted to create while one was already in flight. Sent when
   *  that one finishes, and dropped outright if the prospect starts talking. */
  pendingResponse: { instructions?: string; isClarification?: boolean } | null;
  /** The assistant's spoken words on the current response, accumulated from
   *  transcript deltas. Read for the pending question and the ack guard. */
  assistantTurnText: string;
  /** True when the response now streaming was created for a turn the gate
   *  ruled produced no answer — so an acknowledgement in it is a violation. */
  currentResponseIsClarification: boolean;
}

/**
 * Imogen's first words on a connected outbound call. She speaks first (the model
 * does not wait for the prospect to say hello), states the life-insurance purpose
 * plainly, discloses she is an AI, and confirms identity — short, under 20 words.
 */
export function openingLine(lead: LeadContext, agentDisplayName: string): string {
  const name = lead.firstName?.trim();
  if (name) {
    return `Hi, this is ${agentDisplayName}, an AI assistant calling about life insurance options. Am I speaking with ${name}?`;
  }
  return `Hi, this is ${agentDisplayName}, an AI assistant calling about life insurance options. Is now an okay time?`;
}

/** Injectable so tests can drive a call without an OpenAI connection. */
export type OpenAiSocketFactory = (
  url: string,
  headers: Record<string, string>
) => WebSocket;

const defaultOpenAiSocketFactory: OpenAiSocketFactory = (url, headers) =>
  new WebSocket(url, { headers });

export class RealtimeHandler {
  private sessions = new Map<string, ActiveSession>();
  private bridge: KafkaBridge;
  private createOpenAiSocket: OpenAiSocketFactory;
  private reaperTimer: ReturnType<typeof setInterval> | null = null;

  constructor(
    bridge: KafkaBridge,
    createOpenAiSocket: OpenAiSocketFactory = defaultOpenAiSocketFactory
  ) {
    this.bridge = bridge;
    this.createOpenAiSocket = createOpenAiSocket;
  }

  async createSession(sessionId: string, lead: LeadContext): Promise<void> {
    const actor = createActor(salesMachine, { input: { lead } });
    actor.start();

    const session: ActiveSession = {
      sessionId,
      lead,
      actor,
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
      telephonyBound: false,
      realtimeReady: false,
      openingSent: false,
      closing: false,
      openaiWs: null,
      telephonyWs: null,
      streamSid: null,
      startedAt: Date.now(),
      awaitingResponseSince: null,
      consecutiveStalls: 0,
      watchdogTimer: null,
      turnTiming: null,
      assistantSpeaking: false,
      userTranscripts: [],
      captureEvidence: {},
      lastSpeechEndedAt: null,
      transcriptionEverSeen: false,
      transcriptWaiters: [],
      requestTranscriptLogprobs: loadConfig().transcribeLogprobs,
      waitForTranscriptSince: async () => {},
      slots: {},
      trace: new CallTrace(sessionId, Date.now()),
      turnVerdict: null,
      pendingQuestion: null,
      gateSeq: 0,
      gateOpen: null,
      consecutiveNotHeard: 0,
      skippedQuestions: [],
      skippedSteps: new Set(),
      responseInFlight: false,
      pendingResponse: null,
      assistantTurnText: "",
      currentResponseIsClarification: false,
    };
    session.waitForTranscriptSince = (sinceMs, timeoutMs) =>
      this.awaitTranscriptSince(session, sinceMs, timeoutMs);

    this.sessions.set(sessionId, session);
    sessionsActive.inc();
    sessionsCreated.inc();
    sessionsTotal.inc({ outcome: "started" });

    try {
      await this.connectOpenAI(session);
    } catch (err) {
      // The call never connected: reclaim the slot without emitting call_ended
      // (telephony never bound), so no phantom call is recorded.
      logger.error({ err, sessionId }, "session setup failed");
      this.discard(session, "setup_failed");
      throw err;
    }
  }

  private async connectOpenAI(session: ActiveSession): Promise<void> {
    const cfg = loadConfig();
    const ws = this.createOpenAiSocket(
      `wss://api.openai.com/v1/realtime?model=${cfg.realtimeModel}`,
      { Authorization: `Bearer ${cfg.openaiApiKey}` }
    );

    session.openaiWs = ws;

    ws.on("open", () => {
      this.pushSessionUpdate(session);
    });

    ws.on("message", (data) => {
      const msg = JSON.parse(data.toString());
      this.handleOpenAiEvent(session, msg);
    });

    ws.on("close", () => {
      logger.info({ sessionId: session.sessionId }, "openai ws closed");
      // Our own teardown removes these listeners before closing, so reaching
      // here means the socket dropped on its own — reclaim the session.
      void this.endSession(session.sessionId, "openai_closed");
    });

    ws.on("error", (err) => {
      logger.error({ err, sessionId: session.sessionId }, "openai ws error");
      realtimeReconnects.inc();
      void this.endSession(session.sessionId, "openai_error");
    });
  }

  bindTelephony(sessionId: string, telephonyWs: WebSocket): boolean {
    const session = this.sessions.get(sessionId);
    if (!session) return false;
    session.telephonyWs = telephonyWs;
    session.telephonyBound = true;

    telephonyWs.on("message", (data) => {
      const msg = JSON.parse(data.toString());
      if (msg.event === "start") {
        // The media stream has started; outbound audio can now reach the prospect.
        session.streamSid = msg.streamSid;
        this.maybeSendOpening(session);
        return;
      }
      if (msg.event === "media" && session.openaiWs?.readyState === WebSocket.OPEN) {
        session.openaiWs.send(
          JSON.stringify({
            type: "input_audio_buffer.append",
            audio: msg.media.payload,
          })
        );
      }
    });

    telephonyWs.on("close", () => {
      this.endSession(sessionId, "telephony_disconnected");
    });

    return true;
  }

  /**
   * Imogen speaks first. Once the Realtime session is ready AND the media stream
   * has started (so audio can actually reach the prospect), send exactly one
   * opening — she does not wait for the prospect to say hello. Guarded by
   * openingSent so repeated readiness events (every session.update, a re-`start`)
   * never send a second greeting.
   */
  private maybeSendOpening(session: ActiveSession): void {
    if (session.openingSent) return;
    if (!session.realtimeReady) return;
    if (!session.streamSid) return;
    if (session.openaiWs?.readyState !== WebSocket.OPEN) return;

    session.openingSent = true;
    const cfg = loadConfig();
    const opening = openingLine(session.lead, cfg.agentDisplayName);
    this.requestResponse(session, {
      instructions:
        "This is the very first thing you say on the call — before the prospect " +
        "speaks. Say exactly this, word for word, then stop and wait for their " +
        `answer. Do not add anything else: "${opening}"`,
    });
    logger.info({ sessionId: session.sessionId }, "sent outbound opening");
  }

  // ── One response at a time ──────────────────────────────────────────────────
  //
  // "Do not queue multiple questions while the user is speaking" is enforced
  // structurally rather than by asking the model nicely: exactly one response
  // may be generating at any moment, and anything that wants to start another
  // waits its turn. Without this, a tool result landing while the previous
  // response is still streaming produces two overlapping utterances — the
  // model reading ahead into its next question over the top of the last one.

  /**
   * Ask the model to speak. Sends immediately when nothing is generating,
   * otherwise parks the request until the current response finishes.
   *
   * `instructions` REPLACES the session instructions for that one response.
   * That is deliberate where it is used (the opening, a clarification, a
   * stall nudge): those are turns with exactly one valid utterance, and the
   * sales script is what pulls the model toward producing a different one.
   * The ordinary path passes nothing, so the system prompt stays in force.
   */
  private requestResponse(
    session: ActiveSession,
    opts: { instructions?: string; isClarification?: boolean; force?: boolean } = {}
  ): void {
    if (session.closing) return;
    if (session.openaiWs?.readyState !== WebSocket.OPEN) return;

    // The watchdog fires precisely BECAUSE nothing is generating — a response
    // that was started and never produced an event is not holding the floor,
    // it has dropped it. Parking the recovery behind it would mean the one
    // mechanism that rescues a dead line can never run on the calls that need
    // it, since response.done is exactly what never arrives.
    if (opts.force) {
      session.responseInFlight = false;
      session.pendingResponse = null;
    }

    if (session.responseInFlight) {
      // Park it WITH its own clarification flag. Setting the session-level flag
      // here would relabel the response currently streaming: a clarification
      // queued behind an ordinary turn would arm the acknowledgement guard
      // against that ordinary turn and cut off a perfectly legitimate
      // "Perfect, thanks" — and an ordinary turn queued behind a clarification
      // would disarm the guard on the turn that most needs it.
      session.pendingResponse = {
        ...(opts.instructions ? { instructions: opts.instructions } : {}),
        isClarification: opts.isClarification === true,
      };
      return;
    }

    session.responseInFlight = true;
    session.assistantTurnText = "";
    session.currentResponseIsClarification = opts.isClarification === true;
    session.trace.responseRequested(opts.isClarification === true);
    session.openaiWs.send(
      JSON.stringify({
        type: "response.create",
        response: opts.instructions ? { instructions: opts.instructions } : {},
      })
    );
    this.markAwaitingResponse(session);
  }

  /** The floor is free. Send whatever was waiting for it, if anything. */
  private releaseResponseFloor(session: ActiveSession): void {
    session.responseInFlight = false;
    session.currentResponseIsClarification = false;
    const pending = session.pendingResponse;
    session.pendingResponse = null;
    if (pending) this.requestResponse(session, pending);
  }

  // ── The turn gate ───────────────────────────────────────────────────────────
  //
  // The whole point: the model does not get to speak until we know whether the
  // prospect actually answered. See discovery/turnGate.ts for the reasoning.

  /**
   * The prospect's turn just ended. Hold the floor while we find out what — if
   * anything — they said. The gate closes the instant this turn's transcript
   * lands, or at the deadline, whichever comes first.
   */
  private openTurnGate(session: ActiveSession): void {
    const cfg = loadConfig();
    this.abortTurnGate(session);

    const seq = ++session.gateSeq;
    const timer = setTimeout(() => this.closeTurnGate(session, seq), cfg.turnGateMaxWaitMs);
    timer.unref?.();
    session.gateOpen = {
      seq,
      openedAt: Date.now(),
      transcript: null,
      confidence: null,
      transcriptionFailed: false,
      timer,
    };
  }

  /** Drop an open gate without ruling — the situation it was judging is gone. */
  private abortTurnGate(session: ActiveSession): void {
    if (!session.gateOpen) return;
    if (session.gateOpen.timer) clearTimeout(session.gateOpen.timer);
    session.gateOpen = null;
  }

  /**
   * Rule on the turn and hand the model its instructions. Idempotent per gate:
   * whichever of the transcript and the deadline arrives first closes it, and
   * the other is a no-op.
   */
  private closeTurnGate(session: ActiveSession, seq: number): void {
    const gate = session.gateOpen;
    if (!gate || gate.seq !== seq) return;      // superseded or already closed
    if (session.closing) return;
    if (gate.timer) clearTimeout(gate.timer);
    session.gateOpen = null;

    const cfg = loadConfig();
    const waitedMs = Date.now() - gate.openedAt;
    turnGateWaitMs.observe(waitedMs);

    const expectation = session.pendingQuestion?.expectation ?? "none";
    let verdict = adjudicateTurn({
      transcript: gate.transcript,
      confidence: gate.confidence,
      transcriptionFailed: gate.transcriptionFailed,
      expectation,
      confidenceMin: cfg.transcriptConfidenceMin,
    });

    // Transcription has produced nothing across SEVERAL turns of this call — it
    // is not late, it is down. Gating every turn on evidence that will never
    // arrive turns the call into Imogen asking the prospect to repeat
    // themselves until they hang up. Same trade-off, and same reasoning, as
    // CAPTURE_REQUIRE_TRANSCRIPT: keep the call usable and make the degradation
    // loud, rather than silently trusting it. Under the strict flag we hold the
    // line instead.
    //
    // The consecutive-turn requirement is what stops this from firing on the
    // first bad turn of a healthy call: one noisy turn before transcription has
    // ever had anything to work on is NOT evidence the transcriber is broken,
    // and treating it as such would open the gate at exactly the moment it is
    // most needed — the opening of the call.
    const transcriptionDown =
      verdict.outcome === "not_heard" &&
      !session.transcriptionEverSeen &&
      session.consecutiveNotHeard >= cfg.turnGateEscalateAfter;
    if (transcriptionDown && !cfg.captureRequireTranscript) {
      logger.error(
        { sessionId: session.sessionId, waitedMs, reason: verdict.reason },
        "turn gate degraded — input transcription has produced nothing on this " +
          "call, so no turn can be verified; the gate is passing turns through " +
          "and every capture on this call is unverified"
      );
      turnGateVerdicts.inc({ outcome: "degraded", reason: "transcription_down" });
      verdict = {
        outcome: "heard_answer",
        notHeardReason: null,
        reason: "transcription unavailable on this call — turn not verified",
        confidence: null,
        transcript: gate.transcript,
      };
    } else {
      turnGateVerdicts.inc({
        outcome: verdict.outcome,
        reason: verdict.notHeardReason ?? "answered",
      });
    }

    session.turnVerdict = {
      outcome: verdict.outcome,
      reason: verdict.reason,
      expectation,
      at: Date.now(),
    };
    session.trace.gateRuled(
      verdict.outcome,
      verdict.reason,
      expectation,
      session.pendingQuestion?.text ?? null
    );

    // ── The recovery budget ────────────────────────────────────────────────
    //
    // Ask once. Stop. Listen. If the answer is unusable, ONE clarification.
    // Stop. Listen. If it is still unusable, the question is over — skip it,
    // or escalate if this keeps happening. The loop must terminate.
    //
    // not_heard and heard_unclear share the budget deliberately. They differ
    // in what she SAYS (ask again vs read the candidate back), not in what
    // they mean for the call: neither produced a usable answer, and a run of
    // low-confidence turns strands the call exactly as a run of silent ones
    // does. Counting only not_heard — which is what the reset below used to
    // do — meant a prospect on a bad line could never trip the bound at all.
    const unusable = verdict.outcome === "not_heard" || verdict.outcome === "heard_unclear";
    if (unusable) {
      session.consecutiveNotHeard += 1;
      const attempts = session.consecutiveNotHeard;
      const budgetSpent = attempts >= cfg.turnGateMaxAttempts;
      const abandoned = session.pendingQuestion?.text ?? null;

      logger.warn(
        {
          sessionId: session.sessionId,
          waitedMs,
          outcome: verdict.outcome,
          reason: verdict.notHeardReason,
          confidence: verdict.confidence,
          transcript: verdict.transcript,
          expectation,
          attempts,
          maxAttempts: cfg.turnGateMaxAttempts,
          action: budgetSpent ? "skip" : "clarify",
        },
        budgetSpent
          ? "turn gate: recovery budget spent — abandoning the question rather than asking again"
          : "turn gate: no usable answer — one clarification, then this question is over"
      );

      if (!budgetSpent) {
        // The single retry. Which line she uses depends on whether there is a
        // candidate to read back or nothing at all.
        this.requestResponse(session, {
          isClarification: true,
          instructions:
            verdict.outcome === "heard_unclear"
              ? unclearInstructions({
                  agentDisplayName: cfg.agentDisplayName,
                  expectation,
                  pendingQuestion: abandoned,
                })
              : notHeardInstructions({
                  agentDisplayName: cfg.agentDisplayName,
                  reason: verdict.notHeardReason!,
                  reasonText: verdict.reason,
                  expectation,
                  pendingQuestion: abandoned,
                  consecutiveFailures: attempts,
                  escalateAfter: cfg.turnGateEscalateAfter,
                }),
        });
        return;
      }

      // Budget spent. The question is abandoned: the counter resets so the
      // NEXT question gets its own full budget, and the pending question is
      // cleared so no later turn is judged against a question that is over.
      //
      // The discovery step is closed too. Without that the flow would compute
      // the same step again on the very next turn — it is derived from what is
      // captured, and an abandoned slot is exactly a slot that is not captured
      // — so she would be handed the question she just gave up on.
      session.consecutiveNotHeard = 0;
      session.pendingQuestion = null;
      const abandonedStep = currentStepId(session, session.lead, session.skippedSteps);
      if (abandonedStep !== "READY_TO_QUOTE") session.skippedSteps.add(abandonedStep);
      session.skippedQuestions.push({
        question: abandoned,
        step: abandonedStep === "READY_TO_QUOTE" ? null : abandonedStep,
        reason: verdict.reason,
        at: Date.now(),
      });
      questionsSkipped.inc({ step: abandonedStep });

      // Closing a step changes where the call is, and the state block is
      // carried on the session instructions — so it has to be resent before
      // the next response, not on the next tool call. Otherwise she is told to
      // move on while still being shown the step she just abandoned.
      this.pushSessionUpdate(session);

      if (session.skippedQuestions.length >= cfg.turnGateEscalateAfter) {
        logger.error(
          {
            sessionId: session.sessionId,
            skipped: session.skippedQuestions.length,
            questions: session.skippedQuestions.map((s) => s.question),
          },
          "turn gate: too many questions abandoned — the line is the problem, escalating"
        );
        turnGateEscalations.inc();
        this.requestResponse(session, {
          isClarification: true,
          instructions: escalateInstructions({
            agentDisplayName: cfg.agentDisplayName,
            skipped: session.skippedQuestions.length,
          }),
        });
        return;
      }

      this.requestResponse(session, {
        isClarification: true,
        instructions: skipQuestionInstructions({
          agentDisplayName: cfg.agentDisplayName,
          pendingQuestion: abandoned,
          attempts,
        }),
      });
      return;
    }

    session.consecutiveNotHeard = 0;

    logger.info(
      {
        sessionId: session.sessionId,
        waitedMs,
        outcome: verdict.outcome,
        confidence: verdict.confidence,
        expectation,
      },
      "turn gate: answer received"
    );
    // The ordinary path — no instruction override, so the full system prompt
    // governs the turn exactly as it always has.
    this.requestResponse(session);
  }

  /**
   * The assistant is speaking. Two things are read from her words: the
   * question she is asking (which sets what shape of answer the next turn must
   * produce), and — on a turn where nothing was heard — whether she has begun
   * thanking the prospect for information nobody has.
   */
  private onAssistantTranscriptDelta(session: ActiveSession, delta: string): void {
    session.assistantTurnText += delta;
    session.trace.assistantSpoke(delta);

    if (!session.currentResponseIsClarification) return;
    if (!ACKNOWLEDGEMENT_OPENING.test(session.assistantTurnText)) return;

    // The gate ruled this turn produced no answer and told her so explicitly,
    // and she opened with an acknowledgement anyway. Cut it off mid-word: a
    // clipped syllable is recoverable, "Perfect, thanks" for an answer that
    // was never given is not.
    session.currentResponseIsClarification = false;
    acknowledgementsBlocked.inc();
    logger.error(
      { sessionId: session.sessionId, spoken: session.assistantTurnText.slice(0, 60) },
      "blocked an acknowledgement on a turn with no confirmed answer"
    );
    this.stopAssistantAudio(session);
    session.pendingResponse = {
      isClarification: true,
      instructions:
        `You started to acknowledge an answer you did not receive. Stop. Do not ` +
        `thank them and do not move on.\n\nSay only this, then stop and listen: ` +
        `"Sorry — I didn't catch that, could you say it once more?"`,
    };
    // The cancel disarmed the watchdog (every response.* event does). Nothing
    // is scheduled to speak until response.done arrives for the cancelled
    // response — so if that never comes, re-arm here rather than leave the
    // prospect on a line where the last thing they heard was a clipped word.
    this.markAwaitingResponse(session);
  }

  /**
   * Record the question the assistant just finished asking, and what shape of
   * answer it calls for. This is what "semantically answers the question"
   * is measured against on the next turn — an ambiguous grunt is not an answer
   * to "are you a smoker?", where it might pass for one after "anything else
   * you'd like me to pass on?".
   *
   * A turn that asked nothing leaves the previous pending question standing:
   * she may have been explaining something, and the question is still owed.
   */
  private notePendingQuestion(session: ActiveSession): void {
    const spoken = session.assistantTurnText.trim();
    session.assistantTurnText = "";
    if (!spoken) return;

    const expectation = classifyQuestion(spoken);
    if (expectation === "none") return;

    const asked = spoken.split("?").slice(-2)[0]?.trim() ?? spoken;
    session.pendingQuestion = {
      text: `${asked}?`.replace(/\?+$/, "?"),
      expectation,
      at: Date.now(),
    };
  }

  /** Flush queued audio at Twilio and cancel generation upstream. */
  private stopAssistantAudio(session: ActiveSession): void {
    if (session.telephonyWs?.readyState === WebSocket.OPEN && session.streamSid) {
      session.telephonyWs.send(
        JSON.stringify({ event: "clear", streamSid: session.streamSid })
      );
    }
    if (session.openaiWs?.readyState === WebSocket.OPEN) {
      session.openaiWs.send(JSON.stringify({ type: "response.cancel" }));
    }
    session.assistantSpeaking = false;
  }

  /**
   * Resolve once a transcript of the prospect's speech finalized at or after
   * `sinceMs` has arrived, or when `timeoutMs` expires — whichever comes first.
   *
   * This exists because input transcription is asynchronous and finishes on
   * its own schedule: the model frequently calls update_lead_field BEFORE the
   * words that justify the capture have been transcribed. Ruling on grounding
   * at that moment judges the capture against evidence that has not arrived
   * yet, which rejects answers the prospect gave perfectly clearly — and
   * sends Imogen back to re-ask a question that was already answered. Waiting
   * here costs nothing on the common path (the transcript is usually already
   * in hand) and bounds the worst case.
   */
  private awaitTranscriptSince(
    session: ActiveSession,
    sinceMs: number,
    timeoutMs: number
  ): Promise<void> {
    const already = session.userTranscripts.some((t) => t.at >= sinceMs);
    if (already || timeoutMs <= 0) return Promise.resolve();

    return new Promise<void>((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        const i = session.transcriptWaiters.indexOf(check);
        if (i >= 0) session.transcriptWaiters.splice(i, 1);
        resolve();
      };
      const check = () => {
        if (session.userTranscripts.some((t) => t.at >= sinceMs)) finish();
      };
      const timer = setTimeout(() => {
        logger.warn(
          { sessionId: session.sessionId, waitedMs: timeoutMs },
          "no transcript for this turn within the wait budget — ruling on the " +
            "evidence available"
        );
        finish();
      }, timeoutMs);
      timer.unref?.();
      session.transcriptWaiters.push(check);
    });
  }

  /** Record a finalized transcript and wake anything waiting on one. */
  private recordTranscript(
    session: ActiveSession,
    text: string,
    confidence: number | null
  ): void {
    session.userTranscripts.push({ text, at: Date.now(), confidence });
    session.transcriptionEverSeen = true;
    // Only real scores are recorded. Substituting a placeholder for an
    // unscored transcript would quietly report confidence the API never gave.
    if (typeof confidence === "number") transcriptConfidence.observe(confidence);
    logger.info(
      { sessionId: session.sessionId, transcript: text, confidence },
      "prospect transcript finalized"
    );
    // Copy: a waiter removes itself from the array as it resolves.
    for (const wake of [...session.transcriptWaiters]) wake();
  }

  // ── Watchdog ────────────────────────────────────────────────────────────────
  //
  // The Realtime API's server-side VAD is expected to auto-generate a response
  // once the prospect stops talking. When that doesn't happen — a missed VAD
  // event, a dropped response, a stuck request — nothing upstream notices, and
  // the call goes silent indefinitely (the prospect has to say "hello?" to
  // continue). This closes that gap: any time we are waiting on the model to
  // speak, a timer is armed; if it fires before the model starts responding,
  // we nudge it once or twice, and if that never recovers, we end the call
  // cleanly rather than leave the prospect on a dead line.

  /** Call the instant we expect the model to speak next. */
  private markAwaitingResponse(session: ActiveSession): void {
    session.awaitingResponseSince = Date.now();
    this.armWatchdog(session);
  }

  /** Call the instant the model has started or finished responding — the
   *  stall risk for this turn is over. */
  private markResponseActivity(session: ActiveSession): void {
    if (session.consecutiveStalls > 0) {
      watchdogStalls.inc({ outcome: "recovered" });
      logger.info(
        { sessionId: session.sessionId, consecutiveStalls: session.consecutiveStalls },
        "watchdog: model resumed responding"
      );
    }
    session.awaitingResponseSince = null;
    session.consecutiveStalls = 0;
    this.clearWatchdogTimer(session);
  }

  private armWatchdog(session: ActiveSession): void {
    this.clearWatchdogTimer(session);
    const cfg = loadConfig();
    const timer = setTimeout(() => this.onWatchdogFire(session), cfg.watchdogStallMs);
    timer.unref?.();
    session.watchdogTimer = timer;
  }

  private clearWatchdogTimer(session: ActiveSession): void {
    if (session.watchdogTimer) {
      clearTimeout(session.watchdogTimer);
      session.watchdogTimer = null;
    }
  }

  private onWatchdogFire(session: ActiveSession): void {
    session.watchdogTimer = null;
    if (session.closing) return;
    if (session.awaitingResponseSince === null) return; // resolved just before firing

    const cfg = loadConfig();
    session.consecutiveStalls += 1;
    const stalledMs = Date.now() - session.awaitingResponseSince;
    const giveUp = session.consecutiveStalls > cfg.watchdogMaxNudges;

    logger.warn(
      {
        sessionId: session.sessionId,
        stalledMs,
        consecutiveStalls: session.consecutiveStalls,
        state: session.actor.getSnapshot().value,
        giveUp,
      },
      "watchdog: model went silent after the prospect finished speaking"
    );
    watchdogStalls.inc({ outcome: giveUp ? "call_ended" : "nudged" });

    if (giveUp) {
      // Recovery attempts exhausted. Do not leave the prospect on a silent
      // line indefinitely, and do not hold the concurrency slot forever.
      void this.endSession(session.sessionId, "watchdog_silence_timeout");
      return;
    }

    // A stall means nothing is generating, whatever the bookkeeping says — so
    // this takes the floor rather than queueing for it.
    this.requestResponse(session, {
          force: true,
          // This nudge fires precisely when the model has gone quiet — and one
          // of the commonest reasons to go quiet is not being sure what the
          // prospect just said. An instruction to "continue with your next
          // question" would therefore push it to invent the answer it was
          // unsure of, turning a recoverable pause into a fabricated fact. So
          // the nudge asks for SOUND, not progress, and names asking them to
          // repeat as a first-class way to comply.
          instructions:
            "You have gone silent and the prospect is waiting on the line. Say " +
            "something now.\n\n" +
            "If you are confident you understood their last answer, carry on " +
            "naturally with your next question or action, without drawing " +
            "attention to the pause.\n\n" +
            "If you are NOT sure what they said — or you went quiet because you " +
            "did not catch it — then ask them to repeat it. That is the correct " +
            "move and it fully satisfies this instruction: \"Sorry, I didn't quite " +
            "catch that — could you say it again?\" Do NOT guess at what they said, " +
            "do NOT treat an unclear answer as given, and do NOT move on to the " +
            "next question to fill the silence. Breaking the silence with an " +
            "invented answer is far worse than the silence itself.",
    });
  }

  private pushSessionUpdate(session: ActiveSession): void {
    const cfg = loadConfig();
    const allowedTools = deriveAllowedTools(session.lead, session);
    const tools = filterToolsByAllowed(allowedTools).map((t) => ({
      type: "function",
      name: t.name,
      description: t.description,
      parameters: t.parameters,
    }));

    // The prompt states the agenda in prose; this appends where the call
    // actually IS, computed from captured slots. The prompt is a description
    // of the job, and it cannot know which question is outstanding on turn 14.
    const systemPrompt =
      buildSystemPrompt(session.lead, {
        agencyName: cfg.agencyName,
        agentDisplayName: cfg.agentDisplayName,
      }) + discoveryStateBlock(session, session.lead, session.skippedSteps);

    session.openaiWs?.send(
      JSON.stringify({
        type: "session.update",
        session: {
          type: "realtime",
          output_modalities: ["audio"],
          instructions: systemPrompt,
          tools,
          tool_choice: "auto",
          audio: {
            input: {
              format: { type: "audio/pcmu" },
              // These values decide when the prospect is judged to have
              // FINISHED a sentence, so they are a correctness setting before
              // they are a speed setting.
              //
              // An earlier pass cut silence_duration_ms to 500ms chasing
              // snappier pacing. That was wrong: ordinary phone speech pauses
              // mid-sentence for longer than that ("I'm... uh... sixty-eight"),
              // so the turn was committed early and the model was handed a
              // FRAGMENT of the answer — then answered as though it had heard
              // the whole thing. Cutting a speaker off is how a system ends up
              // inventing what it did not hear.
              //
              // threshold is back up for noise robustness (a lower value lets
              // background noise open a turn), and prefix_padding is generous
              // so the first word is never clipped. Tunable via env for live
              // adjustment without a rebuild.
              //
              // create_response is OFF while the turn gate is on, and this is
              // the single most important line in this file. VAD ends a turn on
              // SOUND — a cough, a car door, a breath — and with create_response
              // the API then generates a reply from that, before transcription
              // has produced a single word. An audio-native model handed noise
              // does not go quiet; it produces the most probable continuation of
              // a sales call, which is "Perfect, thanks" and the next question.
              // Every check downstream is too late by then: the record can still
              // be kept clean, but the prospect has already been thanked for an
              // answer nobody has. So response creation belongs to the gate,
              // which speaks only once it knows what was actually said.
              turn_detection: {
                type: "server_vad",
                threshold: cfg.vadThreshold,
                prefix_padding_ms: cfg.vadPrefixPaddingMs,
                silence_duration_ms: cfg.vadSilenceDurationMs,
                create_response: !cfg.turnGateEnabled,
                interrupt_response: true,
              },
              // Runs alongside generation (it does not gate the response), so
              // it costs no conversational latency. It is not merely
              // diagnostic: these transcripts are the evidence base every
              // captured slot is checked against, so a call without them can
              // verify nothing.
              transcription: { model: cfg.transcribeModel },
            },
            output: {
              format: { type: "audio/pcmu" },
              voice: cfg.realtimeVoice,
            },
          },
          // Per-token logprobs on input transcription — the only real measure
          // of how sure the transcriber was about the prospect's words, and
          // therefore the only honest basis for "was that clearly heard?".
          // Omitted automatically if the API ever rejects the field, so a
          // capability gap degrades to unscored rather than breaking setup.
          ...(session.requestTranscriptLogprobs
            ? { include: ["item.input_audio_transcription.logprobs"] }
            : {}),
        },
      })
    );
  }

  /**
   * The Realtime API rejected our session config. If it was the logprobs
   * `include` — an optional enrichment — drop it and re-send rather than run
   * the call with no instructions and no tools, which is what an unapplied
   * session.update actually means.
   */
  private handleSessionError(session: ActiveSession, msg: Record<string, unknown>): void {
    if (!session.requestTranscriptLogprobs) return;
    const detail = JSON.stringify(msg["error"] ?? msg);
    if (!/include|logprob/i.test(detail)) return;

    session.requestTranscriptLogprobs = false;
    logger.warn(
      { sessionId: session.sessionId, detail },
      "Realtime API rejected the transcription logprobs request — retrying " +
        "without it; captures on this call will be unscored but still grounded"
    );
    this.pushSessionUpdate(session);
  }

  private handleOpenAiEvent(session: ActiveSession, msg: Record<string, unknown>): void {
    const type = msg["type"] as string;
    if (type === "error" || type.startsWith("session.")) { logger.info({ sessionId: session.sessionId, type, msg }, "openai event"); }
    if (type === "error") {
      this.handleSessionError(session, msg);
    }
    if (type === "session.created" || type === "session.updated") {
      // The Realtime session is ready. Fires on every session.update, so the
      // opening is guarded by openingSent, not this event.
      session.realtimeReady = true;
      this.maybeSendOpening(session);
    }
    // Any response.* event (created, deltas, done, a tool call, even cancelled)
    // is proof the model is not stalled on this turn — this is the watchdog's
    // single clear point, independent of which specific event type it is.
    if (type.startsWith("response.")) {
      this.markResponseActivity(session);
    }
    if (type === "input_audio_buffer.speech_started") {
      // VAD detected the prospect's voice. Recorded before barge-in handling
      // so the breakdown shows when they actually started, whether or not
      // this turned out to be an interruption.
      if (session.turnTiming && session.turnTiming.speechStartedAt === null) {
        session.turnTiming.speechStartedAt = Date.now();
      }
      // The prospect has started talking. If we are mid-utterance, stop dead —
      // a human stops talking the instant they are interrupted.
      session.trace.userSpeechDetected();
      this.handleBargeIn(session);
      // They are still going. Anything the gate was about to rule on is stale,
      // and a response parked behind the last one must never surface on top of
      // them — that is the "reading ahead while the user is speaking" defect.
      this.abortTurnGate(session);
      session.pendingResponse = null;
      if (session.awaitingResponseSince !== null) {
        // They spoke before our nudge fired. Stand down quietly — speech_stopped
        // re-arms once they finish, so we never nudge over them mid-sentence.
        this.clearWatchdogTimer(session);
        session.awaitingResponseSince = null;
      }
    }
    if (type === "conversation.item.input_audio_transcription.delta") {
      // First partial: the earliest moment any of the prospect's words were
      // recognized. Distinguishes a transcriber slow to START from one slow
      // to FINISH — they point at different problems.
      if (session.turnTiming && session.turnTiming.firstTranscriptPartialAt === null) {
        session.turnTiming.firstTranscriptPartialAt = Date.now();
      }
    } else if (type === "conversation.item.input_audio_transcription.completed") {
      if (session.turnTiming && session.turnTiming.transcriptCompletedAt === null) {
        session.turnTiming.transcriptCompletedAt = Date.now();
      }
      // The prospect's own words, finalized. This is the evidence base every
      // captured slot is checked against — see update_lead_field.
      const raw = String(msg["transcript"] ?? "");
      const text = raw.trim();
      const confidence = confidenceFromLogprobs(
        msg["logprobs"] as TranscriptLogprob[] | undefined
      );
      session.trace.transcriptFinalized(raw, confidence);
      if (text) this.recordTranscript(session, text, confidence);
      // This turn's words are in. The gate can rule now rather than sit out
      // its full budget — on the common path that is the whole added latency.
      // The RAW transcript is handed over, not the trimmed one: an empty or
      // whitespace-only result is itself the finding.
      if (session.gateOpen) {
        session.gateOpen.transcript = raw;
        session.gateOpen.confidence = confidence;
        this.closeTurnGate(session, session.gateOpen.seq);
      }
    } else if (type === "conversation.item.input_audio_transcription.failed") {
      // The transcriber could not make out the audio. That is a direct signal
      // that nothing reliable was heard this turn — leaving it unlogged would
      // let the model's own account of the turn stand unchallenged.
      logger.warn(
        { sessionId: session.sessionId, error: msg["error"] },
        "input transcription failed — no verified words for this turn"
      );
      if (session.gateOpen) {
        session.gateOpen.transcriptionFailed = true;
        this.closeTurnGate(session, session.gateOpen.seq);
      }
    } else if (type === "response.created") {
      session.trace.responseStarted();
      if (session.turnTiming && session.turnTiming.responseCreatedAt === null) {
        session.turnTiming.responseCreatedAt = Date.now();
      }
    } else if (type === "response.output_audio.delta") {
      const delta = msg["delta"] as string | undefined;
      if (delta && session.telephonyWs?.readyState === WebSocket.OPEN) {
        session.telephonyWs.send(
          JSON.stringify({ event: "media", streamSid: session.streamSid, media: { payload: delta } })
        );
        session.assistantSpeaking = true;
        if (session.turnTiming && session.turnTiming.firstAudioPacketSentAt === null) {
          session.turnTiming.firstAudioPacketSentAt = Date.now();
          silenceToFirstAudioMs.observe(
            session.turnTiming.firstAudioPacketSentAt - session.turnTiming.speechStoppedAt
          );
        }
      }
    } else if (
      // Both spellings: the API renamed this event alongside output_modalities,
      // and reading only one silently loses the assistant's own words — which
      // are what the pending question and the acknowledgement guard read.
      type === "response.audio_transcript.delta" ||
      type === "response.output_audio_transcript.delta"
    ) {
      const delta = msg["delta"] as string | undefined;
      if (delta) {
        if (session.turnTiming && session.turnTiming.firstTranscriptDeltaAt === null) {
          session.turnTiming.firstTranscriptDeltaAt = Date.now();
        }
        this.onAssistantTranscriptDelta(session, delta);
        // With exact options on the table, a vague premium range ("$60 to $80")
        // is a policy violation — the engine's specific numbers must be spoken.
        const check = policy.evaluateUtterance(delta, {
          hasExactQuote: (session.quoteOptions?.length ?? 0) > 0,
        });
        if (!check.allowed) {
          logger.warn({ reason: check.reason, sessionId: session.sessionId }, "policy abort streaming");
          session.openaiWs?.send(JSON.stringify({ type: "response.cancel" }));
        }
      }
    } else if (type === "response.done") {
      turnsTotal.inc({ role: "assistant" });
      session.trace.assistantFinished();
      session.assistantSpeaking = false;
      // Whatever she just asked sets what the NEXT turn has to produce. Read
      // from her actual words rather than the agenda: the agenda says which
      // fields are outstanding, not which one she chose to ask about just now.
      this.notePendingQuestion(session);
      this.finalizeTurn(session, "completed");
      this.releaseResponseFloor(session);
    } else if (type === "input_audio_buffer.speech_stopped") {
      turnsTotal.inc({ role: "user" });
      // The boundary a capture's evidence must post-date: a slot recorded now
      // has to be justified by words transcribed from THIS turn, not by
      // something the prospect said several questions ago.
      session.lastSpeechEndedAt = Date.now();
      session.trace.userSpeechEnded();
      if (session.turnTiming) session.turnTiming.speechEndedAt = Date.now();
      // A previous turn never got a response.done (the prospect started
      // talking again first, i.e. they interrupted, or something else cut it
      // short) — log what we have rather than silently dropping it.
      if (session.turnTiming) this.finalizeTurn(session, "interrupted");
      session.turnTiming = newTurnTiming();
      // The prospect just finished talking. Arm the watchdog either way — it
      // sits well above the gate's budget, so it only fires if the gate itself
      // never rules, and it stays the backstop when the gate is disabled.
      this.markAwaitingResponse(session);
      if (loadConfig().turnGateEnabled) {
        // Hold the floor until we know whether they actually answered.
        this.openTurnGate(session);
      }
    } else if (type === "response.function_call_arguments.done") {
      const tt = session.turnTiming;
      if (tt && tt.functionCallDetectedAt === null) {
        tt.functionCallDetectedAt = Date.now();
        tt.toolName = (msg["name"] as string) ?? null;
        // Nothing audible happened before this tool call arrived — the
        // prospect is about to sit in silence while it runs. This is the
        // exact "silent tool wait" defect: the model should have said
        // something short before going quiet to fetch/compute.
        if (tt.firstTranscriptDeltaAt === null && tt.firstAudioPacketSentAt === null) {
          const gapMs = tt.functionCallDetectedAt - tt.speechStoppedAt;
          silentToolWaitMs.observe(gapMs);
          logger.warn(
            { sessionId: session.sessionId, toolName: tt.toolName, gapMs },
            "tool call with no spoken acknowledgement first — prospect hears dead air"
          );
        }
      }
      this.handleFunctionCall(session, msg);
    }
  }

  /**
   * The prospect started speaking while the assistant was talking. Stop
   * immediately, the way a person would.
   *
   * Cancelling generation at OpenAI is NOT sufficient on its own. Audio deltas
   * are forwarded to Twilio as fast as the model produces them, and Twilio
   * buffers that audio and plays it back in real time. The model can therefore
   * be many seconds "ahead" of what the prospect is actually hearing. Without
   * an explicit `clear`, Twilio keeps playing everything already queued —
   * which is exactly the "it talks over me and ignores the interruption"
   * behaviour, and it also makes replies feel wildly late, because the
   * prospect is answering audio from several seconds ago.
   *
   * So: flush Twilio's queue first (stops sound reaching the prospect now),
   * then cancel generation upstream.
   */
  private handleBargeIn(session: ActiveSession): void {
    if (!session.assistantSpeaking) return;
    const at = Date.now();
    session.assistantSpeaking = false;

    this.stopAssistantAudio(session);
    // Whatever she was part-way through saying is abandoned, so it is not the
    // pending question — and nothing queued behind it may now be spoken.
    session.assistantTurnText = "";
    session.pendingResponse = null;

    const tt = session.turnTiming;
    if (tt && tt.bargeInAt === null) {
      tt.bargeInAt = at;
      tt.audioFlushedAt = Date.now();
    }
    bargeIns.inc();
    logger.info({ sessionId: session.sessionId }, "barge-in: prospect interrupted, audio flushed");
  }

  /**
   * Log and record metrics for one complete (or interrupted) turn, then clear
   * turnTiming so the next speech_stopped starts fresh. This is the single
   * source of the per-turn latency breakdown: exactly where the time went
   * between the prospect going quiet and the assistant answering.
   */
  private finalizeTurn(session: ActiveSession, outcome: "completed" | "interrupted"): void {
    const tt = session.turnTiming;
    if (!tt) return;
    session.turnTiming = null;

    const responseDoneAt = outcome === "completed" ? Date.now() : null;
    const totalMs = (responseDoneAt ?? tt.firstAudioPacketSentAt ?? Date.now()) - tt.speechStoppedAt;
    if (outcome === "completed") turnTotalMs.observe(totalMs);

    const since = (t: number | null) => (t === null ? null : t - tt.speechStoppedAt);
    logger.info(
      {
        sessionId: session.sessionId,
        outcome,
        toolName: tt.toolName,
        // Every stage as "ms after the prospect stopped talking" — directly
        // shows where the gap is, without needing to diff timestamps by hand.
        speechEndedMs: since(tt.speechEndedAt),
        speechStartedMs: since(tt.speechStartedAt),
        bargeInMs: since(tt.bargeInAt),
        audioFlushedMs: since(tt.audioFlushedAt),
        // How long between the prospect starting to talk over us and the
        // queued audio actually being cut. This is the "does it stop when I
        // interrupt" number.
        bargeInToFlushMs:
          tt.bargeInAt !== null && tt.audioFlushedAt !== null
            ? tt.audioFlushedAt - tt.bargeInAt
            : null,
        firstTranscriptPartialMs: since(tt.firstTranscriptPartialAt),
        transcriptCompletedMs: since(tt.transcriptCompletedAt),
        responseCreatedMs: since(tt.responseCreatedAt),
        firstTranscriptDeltaMs: since(tt.firstTranscriptDeltaAt),
        firstAudioPacketSentMs: since(tt.firstAudioPacketSentAt),
        functionCallDetectedMs: since(tt.functionCallDetectedAt),
        toolExecStartMs: since(tt.toolExecStartAt),
        toolExecEndMs: since(tt.toolExecEndAt),
        toolExecDurationMs:
          tt.toolExecStartAt !== null && tt.toolExecEndAt !== null
            ? tt.toolExecEndAt - tt.toolExecStartAt
            : null,
        responseDoneMs: since(responseDoneAt),
        totalMs,
      },
      "turn latency"
    );
  }

  private async handleFunctionCall(
    session: ActiveSession,
    msg: Record<string, unknown>
  ): Promise<void> {
    const toolName = msg["name"] as string;
    const callId = msg["call_id"] as string;
    const rawArgs = msg["arguments"] as string;

    let input: Record<string, unknown>;
    try {
      input = JSON.parse(rawArgs);
    } catch {
      this.deliverToolResult(session, callId, { error: "invalid_json_args" });
      return;
    }

    const handler = toolHandlers[toolName];
    if (!handler) {
      this.deliverToolResult(session, callId, { error: "unknown_tool" });
      return;
    }

    const start = Date.now();
    if (session.turnTiming) session.turnTiming.toolExecStartAt = start;
    const traceIdx = session.trace.toolInvoked(toolName, input);
    try {
      const result = await handler(input, {
        lead: session.lead,
        sessionId: session.sessionId,
        bridge: this.bridge,
        actor: session.actor,
        session,
      });
      turnLatencyMs.observe(Date.now() - start);
      if (session.turnTiming) session.turnTiming.toolExecEndAt = Date.now();
      session.trace.toolReturned(traceIdx, result, Date.now() - start);

      // A terminal tool (end_call) ends the call. Acknowledge the tool result,
      // but do not ask the model to speak again, and reclaim the session — the
      // call is over whether or not the media stream closes afterward.
      const terminal = result["ended"] === true;

      // Refresh the tool list *before* asking the model to respond. A tool that
      // just unlocked the next stage (get_quote → book_appointment) must be
      // callable on this turn, not the one after it: deliverToolResult sends
      // response.create, which begins generation immediately.
      this.pushSessionUpdate(session);
      this.deliverToolResult(session, callId, result, { requestResponse: !terminal });
      this.logSessionSnapshot(session, toolName);

      if (terminal) {
        await this.endSession(session.sessionId, "end_call");
      }
    } catch (err) {
      logger.error({ err, toolName, sessionId: session.sessionId }, "tool handler error");
      this.deliverToolResult(session, callId, { error: String(err) });
    }
  }

  /**
   * Structured, per-turn visibility into where the call actually stands —
   * "current state, collected slots, missing slots, next planned action" is
   * otherwise only inferable by replaying tool_invocations one at a time.
   * Log level is info so it shows up by default without cranking verbosity.
   */
  private logSessionSnapshot(session: ActiveSession, lastTool: string): void {
    // The discovery step, not the xstate value: `greeting` for the whole call
    // is not a fact about the conversation, and a trace whose state column
    // never changes cannot show where a call got stuck.
    session.trace.noteState(
      currentStepId(session, session.lead, session.skippedSteps),
      quoteUnlocked(session.lead, session),
      missingForQuote(session.lead, session)
    );
    logger.info(
      {
        sessionId: session.sessionId,
        lastTool,
        machineState: session.actor.getSnapshot().value,
        remainingAgendaItems: session.lead.acquisitionPlan?.length ?? 0,
        quoteReady: quoteUnlocked(session.lead, session),
        missingForQuote: missingForQuote(session.lead, session),
        // Slots something was said for that were never confirmed. These are
        // the questions still genuinely open: we heard a sound and refused to
        // write down a guess at what it was. Invisible otherwise, because a
        // HEARD slot deliberately reaches neither `captured` nor the brief.
        heardButUnconfirmed: heardButUnconfirmed(session.slots),
        quoteGenerated: session.quote !== null,
        quoteOptionsPresented: session.quoteOptions?.length ?? 0,
        selectedQuoteOption: session.selectedQuoteOption,
        appointmentBooked: session.appointmentBooked,
        dncCaptured: session.dncCaptured,
      },
      "session snapshot"
    );
  }

  private deliverToolResult(
    session: ActiveSession,
    callId: string,
    result: Record<string, unknown>,
    opts: { requestResponse?: boolean } = {}
  ): void {
    session.openaiWs?.send(
      JSON.stringify({
        type: "conversation.item.create",
        item: {
          type: "function_call_output",
          call_id: callId,
          output: JSON.stringify(result),
        },
      })
    );
    if (opts.requestResponse !== false) {
      // Through the floor, not straight at the socket: a tool result landing
      // while the previous response is still streaming would otherwise start a
      // second utterance on top of the first.
      this.requestResponse(session);
    }
  }

  /**
   * Outcome for a call that ended without the model calling end_call — the
   * prospect hung up, or Twilio dropped the stream. brain.py maps this onto a
   * follow-up sequence, so it must name what actually happened.
   */
  private hangupOutcome(session: ActiveSession): string {
    // Checked first. "dnc" is terminal in schedule_for_outcome, so it schedules
    // nothing. Reporting "quoted_not_closed" for someone who asked to be removed
    // would text them three times over the next week.
    if (session.dncCaptured) return "dnc";
    if (session.appointmentBooked) return "appointment_set";
    if (session.quote) return "quoted_not_closed";
    if (session.quoteFailureReason) return "quote_failed";
    return "hangup";
  }

  /**
   * Reclaim a session at a terminal event (end_call, hangup, socket close,
   * reaper). Idempotent: any number of concurrent or repeated terminal events
   * publish call_ended at most once, decrement the active gauge once, and tear
   * the session down once.
   */
  async endSession(sessionId: string, reason?: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) return;                       // already reclaimed
    if (session.closing) {                       // a concurrent terminal event
      duplicateTerminalEvents.inc();
      return;
    }
    session.closing = true;                      // set before the first await

    // The timeline, before anything is torn down. Wrapped because a tracing
    // failure must never be the thing that stops a session being reclaimed.
    try {
      session.trace.flush(reason ?? "unknown");
    } catch (err) {
      logger.error({ err, sessionId }, "call trace flush failed");
    }

    // A call that reached a person and was not already closed by end_call still
    // needs an outcome — otherwise no follow-up or retry is ever scheduled. A
    // call that never bound telephony (never answered, failed setup) records
    // nothing: there was no call.
    if (session.telephonyBound && !session.callEndedPublished) {
      session.callEndedPublished = true;
      try {
        // The connected call's handoff profile rides along — captured facts
        // only, driving Ryan's outcome SMS. A call that never bound telephony
        // publishes nothing, so no profile is ever fabricated for a call that
        // did not happen.
        await this.bridge.publishEvent({
          eventType: "call_ended",
          leadId: session.lead.leadId,
          sessionId,
          outcome: this.hangupOutcome(session),
          durationSeconds: Math.round((Date.now() - session.startedAt) / 1000),
          prospectProfile: handoffProfile(session),
        });
      } catch (err) {
        logger.error({ err, sessionId }, "failed to publish call_ended on session end");
      }
    }

    this.teardown(session);
    this.sessions.delete(sessionId);
    sessionsActive.dec();
    sessionsClosed.inc();

    const durationSec = (Date.now() - session.startedAt) / 1000;
    sessionDurationSec.observe(durationSec);
    logger.info({ sessionId, reason, durationSec }, "session ended");
  }

  /** Reclaim a session that never connected, without emitting call_ended. */
  private discard(session: ActiveSession, reason: string): void {
    if (session.closing) return;
    session.closing = true;
    this.teardown(session);
    if (this.sessions.delete(session.sessionId)) {
      sessionsActive.dec();
      sessionsClosed.inc();
    }
    logger.info({ sessionId: session.sessionId, reason }, "session discarded");
  }

  /** Release every per-session resource: listeners, sockets, actor, buffers. */
  private teardown(session: ActiveSession): void {
    for (const ws of [session.openaiWs, session.telephonyWs]) {
      if (!ws) continue;
      // Remove listeners first so close() cannot re-enter endSession.
      try { ws.removeAllListeners(); } catch { /* noop */ }
      try { ws.close(); } catch { /* noop */ }
    }
    session.openaiWs = null;
    session.telephonyWs = null;
    session.streamSid = null;
    this.clearWatchdogTimer(session);
    try { session.actor.stop(); } catch { /* noop */ }
  }

  /**
   * Reclaim sessions whose terminal callback never arrived — a call nobody
   * answered, or a dropped Twilio status webhook. Without this a stuck session
   * holds a concurrency slot forever.
   */
  async reapAbandoned(nowMs: number = Date.now()): Promise<number> {
    const maxAgeMs = loadConfig().sessionMaxAgeSec * 1000;
    const stale = [...this.sessions.values()].filter(
      (s) => !s.closing && nowMs - s.startedAt > maxAgeMs
    );
    for (const session of stale) {
      sessionsReaped.inc();
      await this.endSession(session.sessionId, "reaped");
    }
    return stale.length;
  }

  startReaper(): void {
    if (this.reaperTimer) return;
    const intervalMs = loadConfig().reaperIntervalSec * 1000;
    this.reaperTimer = setInterval(() => {
      void this.reapAbandoned().catch((err) =>
        logger.error({ err }, "reaper sweep failed")
      );
    }, intervalMs);
    this.reaperTimer.unref?.();
  }

  stopReaper(): void {
    if (this.reaperTimer) {
      clearInterval(this.reaperTimer);
      this.reaperTimer = null;
    }
  }

  /** Test/introspection accessor for the live session count. */
  activeSessionCount(): number {
    return this.sessions.size;
  }

  getSession(sessionId: string): { id: string; state: string; lead: LeadContext } | null {
    const s = this.sessions.get(sessionId);
    if (!s) return null;
    return {
      id: s.sessionId,
      state: s.actor.getSnapshot().value as string,
      lead: s.lead,
    };
  }
}
