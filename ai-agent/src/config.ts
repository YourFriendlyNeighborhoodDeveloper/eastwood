import { z } from "zod";

const ConfigSchema = z.object({
  agentDatabaseUrl: z.string().url(),
  kafkaBrokers: z.string().min(1),
  openaiApiKey: z.string().default(""),
  eastwoodCoreBaseUrl: z.string().url().default("http://eastwood-core:8080"),
  agentDisplayName: z.string().default("Alex"),
  agencyName: z.string().default("Eastwood Financial"),
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
    agentDisplayName: process.env["AGENT_DISPLAY_NAME"],
    agencyName: process.env["AGENCY_NAME"],
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
  });
  return _config;
}
