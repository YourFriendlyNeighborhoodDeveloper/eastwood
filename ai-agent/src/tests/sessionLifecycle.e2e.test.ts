/**
 * Session reclamation.
 *
 * Every call must return its slot: at end_call, at hangup, on an OpenAI drop,
 * on a failed setup, and — if the terminal callback never arrives — via the
 * reaper. Reclamation is idempotent: duplicate terminal events publish
 * call_ended at most once and decrement the active gauge once.
 *
 * The primary leak this closes: _initiate_call creates the session before Twilio
 * rings, so a call nobody answers never binds telephony and never fires a close.
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["SESSION_MAX_AGE_SEC"] = "600";

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, describe, it } from "node:test";

import type { KafkaBridge } from "../bridge/kafkaBridge.js";
import type { LeadContext } from "../types.js";

type RealtimeHandlerCtor = typeof import("../realtime/realtimeHandler.js").RealtimeHandler;
let RealtimeHandler: RealtimeHandlerCtor;
let metrics: typeof import("../observability.js");

// ── Fakes ─────────────────────────────────────────────────────────────────────

class FakeOpenAiSocket extends EventEmitter {
  readyState = 1;
  closed = false;
  send(): void {}
  close(): void {
    this.closed = true;
    this.readyState = 3;
    this.emit("close");
  }
  /** Simulate the OpenAI side dropping the connection. */
  drop(): void {
    this.readyState = 3;
    this.emit("close");
  }
  fail(err = new Error("openai unreachable")): void {
    this.emit("error", err);
  }
}

class FakeTelephonySocket extends EventEmitter {
  readyState = 1;
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
  hangUp(): void {
    this.readyState = 3;
    this.emit("close");
  }
}

class FakeBridge {
  readonly events: Array<Record<string, any>> = [];
  async publishEvent(event: Record<string, any>): Promise<void> {
    this.events.push(event);
  }
  count(type: string): number {
    return this.events.filter((e) => e["eventType"] === type).length;
  }
}

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

async function metricValue(metric: any): Promise<number> {
  const m = await metric.get();
  return (m.values ?? []).reduce((sum: number, v: any) => sum + v.value, 0);
}

async function waitFor(predicate: () => boolean, label: string, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for: ${label}`);
    await new Promise((r) => setTimeout(r, 2));
  }
}

/** Start a session with a controllable OpenAI socket. Optionally bind telephony. */
async function startCall(bind = true): Promise<{
  handler: InstanceType<RealtimeHandlerCtor>;
  bridge: FakeBridge;
  openai: FakeOpenAiSocket;
  tele: FakeTelephonySocket;
  sessionId: string;
}> {
  const bridge = new FakeBridge();
  const openai = new FakeOpenAiSocket();
  const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => openai as any);
  const sessionId = randomUUID();

  await handler.createSession(sessionId, makeLead());
  const tele = new FakeTelephonySocket();
  if (bind) handler.bindTelephony(sessionId, tele as any);

  return { handler, bridge, openai, tele, sessionId };
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("session lifecycle", () => {
  before(async () => {
    RealtimeHandler = (await import("../realtime/realtimeHandler.js")).RealtimeHandler;
    metrics = await import("../observability.js");
  });

  let createdBefore = 0;
  let closedBefore = 0;
  let reapedBefore = 0;
  let dupBefore = 0;
  beforeEach(async () => {
    createdBefore = await metricValue(metrics.sessionsCreated);
    closedBefore = await metricValue(metrics.sessionsClosed);
    reapedBefore = await metricValue(metrics.sessionsReaped);
    dupBefore = await metricValue(metrics.duplicateTerminalEvents);
  });

  it("removes the session on a normal end_call", async () => {
    const { handler, sessionId } = await startCall();

    await handler.endSession(sessionId, "end_call");

    assert.equal(handler.getSession(sessionId), null);
    assert.equal(handler.activeSessionCount(), 0);
    assert.equal((await metricValue(metrics.sessionsClosed)) - closedBefore, 1);
  });

  it("removes the session when the prospect hangs up", async () => {
    const { handler, tele, bridge, sessionId } = await startCall();

    tele.hangUp();
    await waitFor(() => handler.getSession(sessionId) === null, "session removed after hangup");

    assert.equal(handler.activeSessionCount(), 0);
    assert.equal(bridge.count("call_ended"), 1, "a call that reached a person records an outcome");
  });

  it("removes the session when the OpenAI socket disconnects", async () => {
    const { handler, openai, sessionId } = await startCall();

    openai.drop();
    await waitFor(() => handler.getSession(sessionId) === null, "session removed after openai drop");

    assert.equal(handler.activeSessionCount(), 0);
  });

  it("leaves no session behind when setup fails", async () => {
    const bridge = new FakeBridge();
    const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => {
      throw new Error("openai handshake failed");
    });
    const sessionId = randomUUID();

    await assert.rejects(handler.createSession(sessionId, makeLead()));

    assert.equal(handler.getSession(sessionId), null);
    assert.equal(handler.activeSessionCount(), 0);
    // A call that never connected records nothing.
    assert.equal(bridge.count("call_ended"), 0);
    assert.equal((await metricValue(metrics.sessionsCreated)) - createdBefore, 1);
    assert.equal((await metricValue(metrics.sessionsClosed)) - closedBefore, 1);
  });

  it("emits exactly one call_ended for duplicate concurrent terminal events", async () => {
    const { handler, bridge, sessionId } = await startCall();

    // Telephony close and OpenAI close land in the same tick.
    await Promise.all([
      handler.endSession(sessionId, "telephony_disconnected"),
      handler.endSession(sessionId, "openai_closed"),
    ]);

    assert.equal(bridge.count("call_ended"), 1, "one outcome for one call");
    assert.equal(handler.activeSessionCount(), 0);
    assert.equal(
      (await metricValue(metrics.duplicateTerminalEvents)) - dupBefore,
      1,
      "the second terminal event is counted as a duplicate",
    );
    assert.equal(
      (await metricValue(metrics.sessionsClosed)) - closedBefore,
      1,
      "the active gauge is decremented once",
    );
  });

  it("does not double-count when end_call is followed by a socket close", async () => {
    const { handler, bridge, tele, openai, sessionId } = await startCall();

    await handler.endSession(sessionId, "end_call");
    // The sockets close a moment later — both must be no-ops.
    tele.hangUp();
    openai.drop();
    await new Promise((r) => setTimeout(r, 20));

    assert.equal(bridge.count("call_ended"), 1);
    assert.equal(handler.activeSessionCount(), 0);
  });

  it("reaps an abandoned session whose terminal callback never arrives", async () => {
    // A call nobody answered: created, never bound, never closed.
    const { handler, bridge, sessionId } = await startCall(false);
    assert.equal(handler.activeSessionCount(), 1);

    // Nothing has happened yet — a fresh session is not reaped.
    assert.equal(await handler.reapAbandoned(Date.now()), 0);
    assert.equal(handler.activeSessionCount(), 1);

    // Past the age cap, it is reclaimed.
    const reaped = await handler.reapAbandoned(Date.now() + 601_000);
    assert.equal(reaped, 1);
    assert.equal(handler.getSession(sessionId), null);
    assert.equal(handler.activeSessionCount(), 0);
    assert.equal((await metricValue(metrics.sessionsReaped)) - reapedBefore, 1);
    // An unanswered call records no outcome.
    assert.equal(bridge.count("call_ended"), 0);
  });

  it("returns active_sessions to zero after 500 sequential calls via mixed terminal paths", async () => {
    const bridge = new FakeBridge();
    const sockets: FakeOpenAiSocket[] = [];
    const handler = new RealtimeHandler(bridge as unknown as KafkaBridge, () => {
      const s = new FakeOpenAiSocket();
      sockets.push(s);
      return s as any;
    });

    const activeBefore = await metricValue(metrics.sessionsActive);

    for (let i = 0; i < 500; i++) {
      const sessionId = randomUUID();
      await handler.createSession(sessionId, makeLead());
      const openai = sockets[sockets.length - 1]!;
      const tele = new FakeTelephonySocket();
      handler.bindTelephony(sessionId, tele as any);

      // Rotate through every terminal path.
      switch (i % 4) {
        case 0:
          await handler.endSession(sessionId, "end_call");
          break;
        case 1:
          tele.hangUp();
          break;
        case 2:
          openai.drop();
          break;
        case 3:
          await handler.reapAbandoned(Date.now() + 601_000);
          break;
      }
      await waitFor(() => handler.getSession(sessionId) === null, `call ${i} reclaimed`);
    }

    assert.equal(handler.activeSessionCount(), 0, "no session left behind");
    assert.equal(
      await metricValue(metrics.sessionsActive),
      activeBefore,
      "the active gauge returns to its starting value",
    );
  });
});
