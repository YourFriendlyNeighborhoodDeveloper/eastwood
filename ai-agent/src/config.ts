import { z } from "zod";

const ConfigSchema = z.object({
  agentDatabaseUrl: z.string().url(),
  kafkaBrokers: z.string().min(1),
  openaiApiKey: z.string().default(""),
  eastwoodCoreBaseUrl: z.string().url().default("http://eastwood-core:8080"),
  // Service credential for eastwood-core's authenticated /api routes. Must
  // match core's EASTWOOD_API_KEY; empty means core will refuse every call.
  eastwoodApiKey: z.string().default(""),
  agentDisplayName: z.string().default("Imogen"),
  agencyName: z.string().default("Eastwood Financial"),
  // "production" forbids quoting on an agent_default face amount.
  appEnv: z.string().default("development"),
  port: z.coerce.number().int().positive().default(8081),
  logLevel: z.string().default("info"),
  topicAgentEvents: z.string().default("agent.events"),
  topicLeadsRouted: z.string().default("leads.routed"),
  topicDlq: z.string().default("dlq.events"),
  // Discrete call-event topics
  topicCallsFieldCaptured: z.string().default("calls.field_captured"),
  topicCallsQuoteRequested: z.string().default("calls.quote_requested"),
  topicCallsQuoteReady: z.string().default("calls.quote_ready"),
  topicCallsQuotePresented: z.string().default("calls.quote_presented"),
  topicCallsObjectionCaptured: z.string().default("calls.objection_captured"),
  topicCallsConsentCaptured: z.string().default("calls.consent_captured"),
  topicCallsDncRequested: z.string().default("calls.dnc_requested"),
  topicCallsLiveTransferRequested: z.string().default("calls.live_transfer_requested"),
  topicCallsEnded: z.string().default("calls.ended"),
  realtimeModel: z.string().default("gpt-4o-realtime-preview"),
  realtimeVoice: z.string().default("alloy"),
  // Reaper: reclaim a session whose terminal callback never arrives (no-answer,
  // dropped webhook). Age cap bounds how long an abandoned call can hold a slot.
  sessionMaxAgeSec: z.coerce.number().int().positive().default(600),
  reaperIntervalSec: z.coerce.number().int().positive().default(30),
  // Watchdog: how long the model may go without starting a response after the
  // prospect finishes speaking (or after we explicitly asked it to respond)
  // before we nudge it. Must sit comfortably above normal turn latency
  // (agent_turn_latency_ms tops out around 5s in practice) so a merely-slow
  // response is never mistaken for a stall.
  watchdogStallMs: z.coerce.number().int().positive().default(8000),
  // Consecutive un-recovered stalls on the same turn before we give up nudging
  // and end the call cleanly rather than leave the prospect on a silent line.
  watchdogMaxNudges: z.coerce.number().int().positive().default(2),
  // Server-VAD turn detection. These decide when the prospect is judged to
  // have finished speaking, so they govern whether the model receives a whole
  // answer or a fragment — a correctness setting first, a pacing one second.
  // 900ms tolerates ordinary mid-sentence phone pauses (500ms did not, and
  // caused fragments to be answered as if complete) while staying well under
  // the original 1500ms. Tunable live without a rebuild.
  vadSilenceDurationMs: z.coerce.number().int().positive().default(900),
  // Higher = less sensitive; guards against background noise opening a turn.
  vadThreshold: z.coerce.number().min(0).max(1).default(0.6),
  // Generous so the prospect's first word is never clipped.
  vadPrefixPaddingMs: z.coerce.number().int().nonnegative().default(500),
  // Input transcription model. Only the gpt-4o-transcribe family returns
  // logprobs, which is the sole real confidence signal available for the
  // prospect's speech — whisper-1 returns none, leaving every capture
  // unscoreable. Overridable so a transcription regression can be rolled back
  // live without a rebuild.
  transcribeModel: z.string().default("gpt-4o-transcribe"),
  // Ask the Realtime API to attach per-token logprobs to input transcription.
  // If a deployment ever rejects the `include` field, the handler falls back
  // automatically; this flag exists to disable the attempt outright.
  transcribeLogprobs: z
    .union([z.boolean(), z.string()])
    .transform((v) => (typeof v === "string" ? v !== "false" && v !== "0" : v))
    .default(true),
  // How long a capture may wait for the current turn's transcription to land
  // before we rule on it. Transcription is asynchronous and routinely arrives
  // AFTER the model has already called update_lead_field, so ruling
  // immediately would judge a capture on evidence that simply had not arrived
  // yet — rejecting answers the prospect gave perfectly clearly. Usually
  // resolves in well under this budget; it is a ceiling, not a delay.
  transcriptWaitMs: z.coerce.number().int().nonnegative().default(2500),
  // Mean per-token probability (exp of the mean logprob) below which a
  // transcription is treated as not clearly heard. Such a capture is not
  // stored; the model is told to confirm the value out loud first.
  // Clean phone speech scores well above this; 0.6 flags genuinely mangled
  // audio without punishing ordinary accents or phone-line noise. Tune live.
  transcriptConfidenceMin: z.coerce.number().min(0).max(1).default(0.6),
  // ── Turn gate ─────────────────────────────────────────────────────────────
  // Server VAD opens and closes a turn on SOUND, not on WORDS: a cough, a car
  // door, or the prospect drawing breath ends a turn. With the API's
  // create_response the model is then asked to reply with no transcript in
  // hand, and an audio-native model handed noise produces the most probable
  // continuation of a sales call — "Perfect, thanks", then the next question.
  //
  // With the gate on, VAD no longer creates responses. The handler waits for
  // the turn's transcript, rules on whether an answer was actually given, and
  // only then tells the model to speak — and what kind of turn it may take.
  // Off restores the old VAD-driven behaviour, as a live rollback.
  turnGateEnabled: z
    .union([z.boolean(), z.string()])
    .transform((v) => (typeof v === "string" ? v !== "false" && v !== "0" : v))
    .default(true),
  // Ceiling on how long the gate waits for THIS turn's finalized transcript
  // before ruling on what it has. This is the only latency the gate adds, and
  // only on the tail: transcription usually finalizes within a few hundred ms
  // of speech ending, and the gate closes the instant it lands. Too low and
  // clearly-spoken answers get sent back for a repeat; too high and a genuine
  // silence costs a long pause before the assistant asks again.
  turnGateMaxWaitMs: z.coerce.number().int().nonnegative().default(1500),
  // How many unusable turns one question gets before it is ABANDONED.
  //
  // The contract is: ask once, stop, listen. If the answer is unusable, say
  // ONE clarification, stop, listen again. If it is still unusable, the
  // question is over — skip the slot and move on, or escalate. So the budget
  // is 2: the original ask plus exactly one retry.
  //
  // This is the bound that makes the loop terminate. Without it the gate is
  // correct on every individual turn and still unusable in aggregate: each
  // ruling says "you did not get an answer, ask again", forever, and a traced
  // call produced five clarifications in a row with no exit. An unknown slot
  // costs a licensed agent one question later; an unbounded retry loop costs
  // the whole call.
  turnGateMaxAttempts: z.coerce.number().int().positive().default(2),
  // Consecutive SKIPPED questions before the problem is treated as the line
  // itself rather than the question. At that point she stops working the
  // agenda, names what is happening, and offers a callback or a handoff —
  // what a person does when they genuinely cannot hear the other party.
  turnGateEscalateAfter: z.coerce.number().int().positive().default(3),
  // What to do when input transcription produces NOTHING for a whole call, so
  // no capture can be verified either way.
  //
  //   false (default) — store the value, mark it unverified, log an error and
  //     list it under "VERIFY BEFORE QUOTING" on the agent's brief.
  //   true            — refuse every capture instead, leaving each slot UNKNOWN.
  //
  // Strict is the stated policy ("UNKNOWN is acceptable, incorrect data is
  // not") and is correct about the data. It is off by default because of what
  // it does to the CALL: with transcription down, Imogen can never record an
  // answer, so she re-asks the same question until the prospect hangs up. The
  // lenient path keeps the call usable and makes every unverified value
  // visible to the human who acts on it, rather than silently trusted.
  captureRequireTranscript: z
    .union([z.boolean(), z.string()])
    .transform((v) => (typeof v === "string" ? v === "true" || v === "1" : v))
    .default(false),
});

export type Config = z.infer<typeof ConfigSchema>;

let _config: Config | null = null;

export function loadConfig(): Config {
  if (_config !== null) return _config;
  _config = ConfigSchema.parse({
    agentDatabaseUrl: process.env["AGENT_DATABASE_URL"],
    kafkaBrokers: process.env["KAFKA_BROKERS"] ?? "localhost:9092",
    openaiApiKey: process.env["OPENAI_API_KEY"],
    eastwoodCoreBaseUrl: process.env["EASTWOOD_CORE_BASE_URL"],
    eastwoodApiKey: process.env["EASTWOOD_API_KEY"],
    agentDisplayName: process.env["AGENT_DISPLAY_NAME"],
    agencyName: process.env["AGENCY_NAME"],
    appEnv: process.env["APP_ENV"],
    port: process.env["PORT"],
    logLevel: process.env["LOG_LEVEL"],
    topicAgentEvents: process.env["TOPIC_AGENT_EVENTS"],
    topicLeadsRouted: process.env["TOPIC_LEADS_ROUTED"],
    topicDlq: process.env["TOPIC_DLQ"],
    topicCallsFieldCaptured: process.env["TOPIC_CALLS_FIELD_CAPTURED"],
    topicCallsQuoteRequested: process.env["TOPIC_CALLS_QUOTE_REQUESTED"],
    topicCallsQuoteReady: process.env["TOPIC_CALLS_QUOTE_READY"],
    topicCallsQuotePresented: process.env["TOPIC_CALLS_QUOTE_PRESENTED"],
    topicCallsObjectionCaptured: process.env["TOPIC_CALLS_OBJECTION_CAPTURED"],
    topicCallsConsentCaptured: process.env["TOPIC_CALLS_CONSENT_CAPTURED"],
    topicCallsDncRequested: process.env["TOPIC_CALLS_DNC_REQUESTED"],
    topicCallsLiveTransferRequested: process.env["TOPIC_CALLS_LIVE_TRANSFER_REQUESTED"],
    topicCallsEnded: process.env["TOPIC_CALLS_ENDED"],
    realtimeModel: process.env["REALTIME_MODEL"],
    realtimeVoice: process.env["REALTIME_VOICE"],
    sessionMaxAgeSec: process.env["SESSION_MAX_AGE_SEC"],
    reaperIntervalSec: process.env["REAPER_INTERVAL_SEC"],
    watchdogStallMs: process.env["WATCHDOG_STALL_MS"],
    watchdogMaxNudges: process.env["WATCHDOG_MAX_NUDGES"],
    vadSilenceDurationMs: process.env["VAD_SILENCE_DURATION_MS"],
    vadThreshold: process.env["VAD_THRESHOLD"],
    vadPrefixPaddingMs: process.env["VAD_PREFIX_PADDING_MS"],
    transcribeModel: process.env["REALTIME_TRANSCRIBE_MODEL"],
    transcribeLogprobs: process.env["REALTIME_TRANSCRIBE_LOGPROBS"],
    transcriptWaitMs: process.env["TRANSCRIPT_WAIT_MS"],
    turnGateEnabled: process.env["TURN_GATE_ENABLED"],
    turnGateMaxWaitMs: process.env["TURN_GATE_MAX_WAIT_MS"],
    turnGateMaxAttempts: process.env["TURN_GATE_MAX_ATTEMPTS"],
    turnGateEscalateAfter: process.env["TURN_GATE_ESCALATE_AFTER"],
    transcriptConfidenceMin: process.env["TRANSCRIPT_CONFIDENCE_MIN"],
    captureRequireTranscript: process.env["CAPTURE_REQUIRE_TRANSCRIPT"],
  });
  return _config;
}
