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
