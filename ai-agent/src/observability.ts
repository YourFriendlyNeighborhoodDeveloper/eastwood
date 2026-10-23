import pino from "pino";
import {
  Counter,
  Gauge,
  Histogram,
  Registry,
} from "prom-client";
import { loadConfig } from "./config.js";

const cfg = loadConfig();

export const logger = pino({
  level: cfg.logLevel,
  transport:
    process.env["NODE_ENV"] !== "production"
      ? { target: "pino-pretty" }
      : undefined,
});

export const registry = new Registry();

export const sessionsActive = new Gauge({
  name: "agent_sessions_active",
  help: "Currently active call sessions",
  registers: [registry],
});

export const sessionsTotal = new Counter({
  name: "agent_sessions_total",
  help: "Total sessions started",
  labelNames: ["outcome"] as const,
  registers: [registry],
});

export const sessionsCreated = new Counter({
  name: "agent_sessions_created_total",
  help: "Total call sessions created",
  registers: [registry],
});

export const sessionsClosed = new Counter({
  name: "agent_sessions_closed_total",
  help: "Total call sessions reclaimed (terminal or reaped)",
  registers: [registry],
});

export const sessionsReaped = new Counter({
  name: "agent_sessions_reaped_total",
  help: "Sessions reclaimed by the abandoned-session reaper",
  registers: [registry],
});

export const duplicateTerminalEvents = new Counter({
  name: "agent_duplicate_terminal_events_total",
  help: "Terminal events for a session already being reclaimed",
  registers: [registry],
});

export const sessionDurationSec = new Histogram({
  name: "agent_session_duration_seconds",
  help: "Call session duration",
  buckets: [10, 30, 60, 120, 300, 600],
  registers: [registry],
});

export const turnsTotal = new Counter({
  name: "agent_turns_total",
  help: "Total conversation turns",
  labelNames: ["role"] as const,
  registers: [registry],
});

export const turnLatencyMs = new Histogram({
  name: "agent_turn_latency_ms",
  help: "Latency from user speech end to first agent audio token",
  buckets: [100, 250, 500, 1000, 2000, 5000],
  registers: [registry],
});

export const toolInvocations = new Counter({
  name: "agent_tool_invocations_total",
  help: "Total tool invocations",
  labelNames: ["tool_name", "outcome"] as const,
  registers: [registry],
});

export const policyDecisions = new Counter({
  name: "agent_policy_decisions_total",
  help: "Total policy engine decisions",
  labelNames: ["severity", "reason"] as const,
  registers: [registry],
});

export const kafkaProduced = new Counter({
  name: "agent_kafka_produced_total",
  help: "Total Kafka messages produced",
  labelNames: ["topic"] as const,
  registers: [registry],
});

export const kafkaConsumeErrors = new Counter({
  name: "agent_kafka_consume_errors_total",
  help: "Total Kafka consumer errors",
  registers: [registry],
});

export const realtimeReconnects = new Counter({
  name: "agent_realtime_reconnects_total",
  help: "Total OpenAI Realtime reconnects",
  registers: [registry],
});

// outcome: nudged | recovered | call_ended
export const watchdogStalls = new Counter({
  name: "agent_watchdog_stalls_total",
  help: "Times the watchdog detected the model had gone silent after the prospect finished speaking",
  labelNames: ["outcome"] as const,
  registers: [registry],
});

// The perceived dead-air gap: prospect finishes talking → first audio token
// back. This is the number that matters for "does it feel conversational" —
// distinct from turnLatencyMs (tool-handler execution time only) and from the
// watchdog (a failure backstop, not a pacing measurement). Buckets run tight
// at the low end because the target is sub-1000ms.
export const silenceToFirstAudioMs = new Histogram({
  name: "agent_silence_to_first_audio_ms",
  help: "Milliseconds from the prospect finishing speaking to the first audio token back",
  buckets: [150, 300, 500, 750, 1000, 1500, 2000, 3000, 5000, 8000],
  registers: [registry],
});

export const turnTotalMs = new Histogram({
  name: "agent_turn_total_ms",
  help: "Milliseconds from the prospect finishing speaking to the assistant finishing this turn's response",
  buckets: [250, 500, 1000, 2000, 3000, 5000, 8000, 12000, 20000],
  registers: [registry],
});

// A tool call detected before any spoken output on this turn — the exact
// "silent tool wait" defect: the model went to fetch/compute something
// without acknowledging first, so the prospect hears nothing while it works.
export const bargeIns = new Counter({
  name: "agent_barge_ins_total",
  help: "Times the prospect interrupted the assistant and queued audio was flushed",
  registers: [registry],
});

// result: grounded | rejected_ungrounded | unverifiable_no_transcript
//       | confirmation_required | confirmed_after_readback
export const slotCaptures = new Counter({
  name: "agent_slot_captures_total",
  help: "Slot capture attempts by whether the value was traceable to the prospect's own transcribed words",
  labelNames: ["result", "capture_key"] as const,
  registers: [registry],
});

// Mean per-token probability of each finalized transcript of the prospect's
// speech, from transcription logprobs. Only real scores are recorded —
// unscored transcripts are absent rather than counted as confident, so a
// drop in this histogram's count means confidence data stopped arriving.
// Buckets cluster high because clean speech scores near 1; the interesting
// region is the tail below the confirmation threshold.
export const transcriptConfidence = new Histogram({
  name: "agent_transcript_confidence",
  help: "Mean per-token probability of finalized prospect transcripts",
  buckets: [0.1, 0.25, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99],
  registers: [registry],
});

export const silentToolWaitMs = new Histogram({
  name: "agent_silent_tool_wait_ms",
  help: "Milliseconds from speech end to a tool call, on turns where nothing was said before the tool call",
  buckets: [100, 250, 500, 1000, 2000, 3000, 5000, 8000],
  registers: [registry],
});

// The turn gate's ruling on each of the prospect's turns, before the assistant
// was allowed to speak. outcome: heard_answer | heard_other | not_heard.
// `reason` names WHY a turn produced no answer (noise_only, low_confidence,
// clipped, ...), which is what separates "the line is bad" from "the VAD
// threshold is too low" from "the transcriber is failing".
export const turnGateVerdicts = new Counter({
  name: "agent_turn_gate_verdicts_total",
  help: "Turn-gate rulings on whether the prospect actually answered",
  labelNames: ["outcome", "reason"] as const,
  registers: [registry],
});

// How long the gate waited for a turn's finalized transcript before ruling.
// This is the only latency the gate adds; the tail against
// TURN_GATE_MAX_WAIT_MS is the number to tune on.
export const turnGateWaitMs = new Histogram({
  name: "agent_turn_gate_wait_ms",
  help: "Milliseconds the turn gate waited for this turn's transcript before ruling",
  buckets: [10, 50, 100, 200, 400, 700, 1000, 1500, 2500],
  registers: [registry],
});

// Slot lifecycle movements: UNKNOWN → HEARD → CONFIRMED. A healthy call is
// mostly UNKNOWN→CONFIRMED; a rise in →HEARD means answers are arriving but
// not clearly enough to trust, and a rise in →UNKNOWN means they are not
// arriving at all.
export const slotTransitions = new Counter({
  name: "agent_slot_transitions_total",
  help: "Slot state transitions by capture key",
  labelNames: ["from", "to", "capture_key"] as const,
  registers: [registry],
});

// The assistant began acknowledging ("Perfect, thanks...") on a turn the gate
// had ruled produced no answer, and the utterance was cut off. This should sit
// at zero: the gate's per-response instructions exist to make it impossible.
// Anything above zero means the model is overriding an explicit instruction.
export const acknowledgementsBlocked = new Counter({
  name: "agent_acknowledgements_blocked_total",
  help: "Assistant acknowledgements cancelled because the slot was not confirmed",
  registers: [registry],
});

// A question asked, clarified once, and still not answered — so it was
// abandoned rather than asked a third time. Expected to be non-zero on real
// calls; a sharp rise means audio quality, not agent behaviour.
export const questionsSkipped = new Counter({
  name: "agent_questions_skipped_total",
  help: "Questions abandoned after exhausting their recovery budget",
  labelNames: ["step"],
  registers: [registry],
});

// Several questions abandoned in a row: the line itself is unusable and the
// call was diverted to a callback or handoff instead of working the agenda.
export const turnGateEscalations = new Counter({
  name: "agent_turn_gate_escalations_total",
  help: "Calls where repeated abandoned questions escalated to a callback or handoff",
  registers: [registry],
});
