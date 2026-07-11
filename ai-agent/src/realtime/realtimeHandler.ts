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
  /** Captured field values (captureKey → value), source of the handoff profile. */
  captured: Record<string, unknown>;
  objections: Array<{ objectionType: string; text: string }>;
  appointment: { appointmentId: string; date: string; time: string } | null;
  dncCaptured: boolean;
  callEndedPublished: boolean;
  /** True once Twilio bound the media stream — i.e. the call reached a person. */
  telephonyBound: boolean;
  /** Set synchronously at the start of reclamation so a second terminal event
   *  cannot double-publish, double-decrement, or re-tear-down. */
  closing: boolean;
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
      appointmentBooked: false,
      captured: {},
      objections: [],
      appointment: null,
      dncCaptured: false,
      callEndedPublished: false,
      telephonyBound: false,
      closing: false,
      openaiWs: null,
      telephonyWs: null,
      streamSid: null,
      startedAt: Date.now(),
    };

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
    const allowedTools = deriveAllowedTools(session.lead, session);
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

      if (terminal) {
        await this.endSession(session.sessionId, "end_call");
      }
    } catch (err) {
      logger.error({ err, toolName, sessionId: session.sessionId }, "tool handler error");
      this.deliverToolResult(session, callId, { error: String(err) });
    }
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
      session.openaiWs?.send(JSON.stringify({ type: "response.create" }));
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

    // A call that reached a person and was not already closed by end_call still
    // needs an outcome — otherwise no follow-up or retry is ever scheduled. A
    // call that never bound telephony (never answered, failed setup) records
    // nothing: there was no call.
    if (session.telephonyBound && !session.callEndedPublished) {
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
