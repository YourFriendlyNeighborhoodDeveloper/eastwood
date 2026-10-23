/**
 * CAPTURE_REQUIRE_TRANSCRIPT — the strict reading of "UNKNOWN is acceptable,
 * incorrect data is not".
 *
 * When input transcription produces nothing for an entire call, no capture can
 * be verified either way. The default keeps the value and flags it, so the call
 * remains usable; this flag refuses instead, leaving every slot UNKNOWN. Lives
 * in its own file because the setting is read at config load, which is
 * process-wide and cached.
 */
process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["CAPTURE_REQUIRE_TRANSCRIPT"] = "true";

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { LeadContext, SessionState } from "../types.js";

let toolHandlers: typeof import("../tools/toolHandlers.js").toolHandlers;

function makeSession(transcripts: string[]): SessionState {
  return {
    lead: {
      leadId: randomUUID(),
      tenantId: "default",
      phoneE164: "+13135550123",
      firstName: "Dolores",
      state: "MI",
      productLine: "final_expense",
      consentStatus: "express_written",
      acquisitionPlan: [],
    } as LeadContext,
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
    userTranscripts: transcripts.map((text) => ({ text, at: Date.now() })),
    captureEvidence: {},
  } as unknown as SessionState;
}

async function capture(
  session: SessionState,
  args: Record<string, unknown>
): Promise<Record<string, unknown>> {
  return toolHandlers["update_lead_field"]!(args, {
    lead: session.lead,
    sessionId: "sess-strict",
    bridge: { publishEvent: async () => {} } as any,
    actor: { send: () => {} } as any,
    session,
  });
}

describe("strict mode leaves the slot UNKNOWN rather than storing unverified", () => {
  before(async () => {
    ({ toolHandlers } = await import("../tools/toolHandlers.js"));
  });

  it("refuses a capture when nothing on the call was transcribed", async () => {
    const session = makeSession([]);
    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "never smoked",
    });

    assert.equal(res["captured"], false);
    assert.equal(res["rejected"], true);
    assert.equal(session.captured["tobacco"], undefined, "the slot must stay UNKNOWN");
    assert.equal(session.captureEvidence["tobacco"], undefined, "no evidence may be recorded");
  });

  it("still stores a value the prospect was actually transcribed saying", async () => {
    // Strict mode tightens the unverifiable case only — it must not break
    // capture on a healthy call.
    const session = makeSession(["No, I've never smoked."]);
    const res = await capture(session, {
      field: "tobacco",
      captureKey: "tobacco",
      value: "none",
      heardVerbatim: "never smoked",
    });

    assert.equal(res["captured"], true);
    assert.equal(session.captured["tobacco"], "none");
  });
});
