import { createActor, type Actor } from "xstate";
import WebSocket from "ws";
import { loadConfig } from "../config.js";
import {
  logger,
  realtimeReconnects,
  sessionsActive,
  sessionsTotal,
  sessionDurationSec,
  turnLatencyMs,
  turnsTotal,
} from "../observability.js";
import { PolicyEngine } from "../policy/policyEngine.js";
import { buildSystemPrompt } from "../prompts/buildSystemPrompt.js";
import { salesMachine } from "../machine/salesMachine.js";
import { filterToolsByAllowed } from "../tools/toolDefinitions.js";
import { deriveAllowedTools } from "../tools/toolGating.js";
import { toolHandlers } from "../tools/toolHandlers.js";
import type { LeadContext, Quote } from "../types.js";
import type { KafkaBridge } from "../bridge/kafkaBridge.js";

const policy = new PolicyEngine();

interface ActiveSession {
  sessionId: string;
  lead: LeadContext;
  actor: Actor<typeof salesMachine>;
  /** Set by the get_quote tool. Drives which closing tools are exposed. */
  quote: Quote | null;
  appointmentBooked: boolean;
  callEndedPublished: boolean;
  openaiWs: WebSocket | null;
  telephonyWs: WebSocket | null;
  streamSid: string | null;
  startedAt: number;
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

  constructor(
    bridge: KafkaBridge,
    createOpenAiSocket: OpenAiSocketFactory = defaultOpenAiSocketFactory
  ) {
    this.bridge = bridge;
    this.createOpenAiSocket = createOpenAiSocket;
  }

  async createSession(sessionId: string, lead: LeadContext): Promise<void> {
    const cfg = loadConfig();
    const actor = createActor(salesMachine, { input: { lead } });
    actor.start();

    const session: ActiveSession = {
      sessionId,
      lead,
      actor,
      quote: null,
      appointmentBooked: false,
      callEndedPublished: false,
      openaiWs: null,
      telephonyWs: null,
      streamSid: null,
      startedAt: Date.now(),
    };

    this.sessions.set(sessionId, session);
    sessionsActive.inc();
    sessionsTotal.inc({ outcome: "started" });

    await this.connectOpenAI(session);
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
    });

    ws.on("error", (err) => {
      logger.error({ err, sessionId: session.sessionId }, "openai ws error");
      realtimeReconnects.inc();
    });
  }

  bindTelephony(sessionId: string, telephonyWs: WebSocket): boolean {
    const session = this.sessions.get(sessionId);
    if (!session) return false;
    session.telephonyWs = telephonyWs;

    telephonyWs.on("message", (data) => {
      if (session.openaiWs?.readyState === WebSocket.OPEN) {
        const msg = JSON.parse(data.toString());
        if (msg.event === "start") { session.streamSid = msg.streamSid; }
        if (msg.event === "media") {
          session.openaiWs.send(
            JSON.stringify({
              type: "input_audio_buffer.append",
              audio: msg.media.payload,
            })
          );
        }
      }
    });

    telephonyWs.on("close", () => {
      this.endSession(sessionId, "telephony_disconnected");
    });

    return true;
  }

  private pushSessionUpdate(session: ActiveSession): void {
    const cfg = loadConfig();
    const allowedTools = deriveAllowedTools(session.lead, session.quote);
    const tools = filterToolsByAllowed(allowedTools).map((t) => ({
      type: "function",
      name: t.name,
      description: t.description,
      parameters: t.parameters,
    }));

    const systemPrompt = buildSystemPrompt(session.lead, {
      agencyName: cfg.agencyName,
      agentDisplayName: cfg.agentDisplayName,
    });

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
              turn_detection: { type: "server_vad", threshold: 0.6, prefix_padding_ms: 700, silence_duration_ms: 1500 },
            },
            output: {
              format: { type: "audio/pcmu" },
              voice: cfg.realtimeVoice,
            },
          },
        },
      })
    );
  }

  private handleOpenAiEvent(session: ActiveSession, msg: Record<string, unknown>): void {
    const type = msg["type"] as string;
    if (type === "error" || type.startsWith("session.")) { logger.info({ sessionId: session.sessionId, type, msg }, "openai event"); }
    if (type === "response.output_audio.delta") {
      const delta = msg["delta"] as string | undefined;
      if (delta && session.telephonyWs?.readyState === WebSocket.OPEN) {
        session.telephonyWs.send(
          JSON.stringify({ event: "media", streamSid: session.streamSid, media: { payload: delta } })
        );
      }
    } else if (type === "response.audio_transcript.delta") {
      const delta = msg["delta"] as string | undefined;
      if (delta) {
        const check = policy.evaluateUtterance(delta);
        if (!check.allowed) {
          logger.warn({ reason: check.reason, sessionId: session.sessionId }, "policy abort streaming");
          session.openaiWs?.send(JSON.stringify({ type: "response.cancel" }));
        }
      }
    } else if (type === "response.done") {
      turnsTotal.inc({ role: "assistant" });
    } else if (type === "input_audio_buffer.speech_stopped") {
      turnsTotal.inc({ role: "user" });
    } else if (type === "response.function_call_arguments.done") {
      this.handleFunctionCall(session, msg);
    }
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
    try {
      const result = await handler(input, {
        lead: session.lead,
        sessionId: session.sessionId,
        bridge: this.bridge,
        actor: session.actor,
        session,
      });
      turnLatencyMs.observe(Date.now() - start);
      // Refresh the tool list *before* asking the model to respond. A tool that
      // just unlocked the next stage (get_quote → book_appointment) must be
      // callable on this turn, not the one after it: deliverToolResult sends
      // response.create, which begins generation immediately.
      this.pushSessionUpdate(session);
      this.deliverToolResult(session, callId, result);
    } catch (err) {
      logger.error({ err, toolName, sessionId: session.sessionId }, "tool handler error");
      this.deliverToolResult(session, callId, { error: String(err) });
    }
  }

  private deliverToolResult(
    session: ActiveSession,
    callId: string,
    result: Record<string, unknown>
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
    session.openaiWs?.send(JSON.stringify({ type: "response.create" }));
  }

  /**
   * Outcome for a call that ended without the model calling end_call — the
   * prospect hung up, or Twilio dropped the stream. brain.py maps this onto a
   * follow-up sequence, so it must name what actually happened.
   */
  private hangupOutcome(session: ActiveSession): string {
    if (session.appointmentBooked) return "appointment_set";
    if (session.quote) return "quoted_not_closed";
    return "hangup";
  }

  async endSession(sessionId: string, reason?: string): Promise<void> {
    const session = this.sessions.get(sessionId);
    if (!session) return;

    // Non-tool endings must still produce an outcome, otherwise the call leaves
    // no record and no follow-up or retry is ever scheduled.
    if (!session.callEndedPublished) {
      session.callEndedPublished = true;
      try {
        await this.bridge.publishEvent({
          eventType: "call_ended",
          leadId: session.lead.leadId,
          sessionId,
          outcome: this.hangupOutcome(session),
          durationSeconds: Math.round((Date.now() - session.startedAt) / 1000),
        });
      } catch (err) {
        logger.error({ err, sessionId }, "failed to publish call_ended on session end");
      }
    }

    session.openaiWs?.close();
    session.telephonyWs?.close();
    session.actor.stop();

    const durationSec = (Date.now() - session.startedAt) / 1000;
    sessionDurationSec.observe(durationSec);
    sessionsActive.dec();

    this.sessions.delete(sessionId);
    logger.info({ sessionId, reason, durationSec }, "session ended");
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
