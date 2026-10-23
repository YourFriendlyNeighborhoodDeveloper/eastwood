/**
 * The quote gate must be legible to the model.
 *
 * get_quote is locked until discovery completes — by design. What must never
 * happen again: the model reading a locked tool as "quoting is unavailable on
 * this call" and telling the prospect only an agent can run numbers. Every
 * update_lead_field result therefore reports quoteReady and the exact missing
 * inputs, and flips to "call get_quote NOW" the moment the gate opens.
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";

import assert from "node:assert/strict";
import { before, describe, it } from "node:test";

import { CAPTURE, missingForQuote, quoteUnlocked } from "../discovery/prospectProfile.js";
import type { LeadContext, SessionState } from "../types.js";

let toolHandlers: typeof import("../tools/toolHandlers.js").toolHandlers;

before(async () => {
  ({ toolHandlers } = await import("../tools/toolHandlers.js"));
});

function makeLead(acquisitionPlan: Array<{ field: string; question: string }> = []): LeadContext {
  return {
    leadId: "1f0e9b8a-0000-4000-8000-000000000001",
    tenantId: "default",
    phoneE164: "+15550001111",
    age: 66,
    state: "MI",
    acquisitionPlan,
  } as unknown as LeadContext;
}

function makeSession(lead: LeadContext, captured: Record<string, unknown>): SessionState {
  return {
    lead,
    quote: null,
    quoteOptions: null,
    selectedQuoteOption: null,
    affordabilityConfirmed: null,
    quoteFailureReason: null,
    offeredSlots: [],
    calendly: null,
    appointmentBooked: false,
    captured,
    objections: [],
    appointment: null,
    dncCaptured: false,
    callEndedPublished: false,
    userTranscripts: [],
    captureEvidence: {},
  } as unknown as SessionState;
}

const FULL_DISCOVERY = {
  [CAPTURE.coveragePurpose]: "burial",
  [CAPTURE.beneficiaryRelationship]: "daughter",
  [CAPTURE.monthlyBudget]: "45",
  [CAPTURE.ratingSex]: "male",
};

function makeCtx(lead: LeadContext, session: SessionState) {
  return {
    lead,
    sessionId: "sess-gate-test",
    bridge: { publishEvent: async () => {} } as never,
    actor: { send: () => {} } as never,
    session,
  };
}

describe("quote gate legibility", () => {
  it("lists every missing input on an empty session", () => {
    const lead = makeLead([{ field: "health_screening", question: "Any conditions?" }]);
    const missing = missingForQuote(lead, makeSession(lead, {}));
    assert.equal(missing.length, 5);
    assert.ok(missing.some((m) => m.includes("coverage purpose")));
    assert.ok(missing.some((m) => m.includes("beneficiary")));
    assert.ok(missing.some((m) => m.includes("monthly budget")));
    assert.ok(missing.some((m) => m.includes("rating sex")));
    assert.ok(missing.some((m) => m.includes("health screening")));
  });

  it("is empty exactly when the gate opens", () => {
    const lead = makeLead([]);
    const session = makeSession(lead, FULL_DISCOVERY);
    assert.deepEqual(missingForQuote(lead, session), []);
    assert.equal(quoteUnlocked(lead, session), true);
  });

  it("a stated budget alone satisfies the amount requirement", () => {
    const lead = makeLead([]);
    const session = makeSession(lead, {
      ...FULL_DISCOVERY,
      [CAPTURE.desiredCoverageAmount]: undefined,
      [CAPTURE.monthlyBudget]: "50",
    });
    assert.ok(!missingForQuote(lead, session).some((m) => m.includes("monthly budget")));
  });

  it("update_lead_field reports the gaps while the gate is closed", async () => {
    const lead = makeLead([]);
    const session = makeSession(lead, {});
    const result = await toolHandlers["update_lead_field"]!(
      { field: "tobacco", captureKey: CAPTURE.tobacco, value: "no" },
      makeCtx(lead, session)
    );
    assert.equal(result["quoteReady"], false);
    const missing = result["missingForQuote"] as string[];
    assert.ok(missing.length >= 4);
    assert.ok(String(result["nextStep"]).includes("Real-time quoting IS available"));
  });

  it("update_lead_field says 'call get_quote NOW' the moment discovery completes", async () => {
    const lead = makeLead([]);
    const session = makeSession(lead, {
      [CAPTURE.coveragePurpose]: "burial",
      [CAPTURE.beneficiaryRelationship]: "daughter",
      [CAPTURE.monthlyBudget]: "45",
    });
    const result = await toolHandlers["update_lead_field"]!(
      { field: "rating_sex", captureKey: CAPTURE.ratingSex, value: "male" },
      makeCtx(lead, session)
    );
    assert.equal(result["quoteReady"], true);
    assert.deepEqual(result["missingForQuote"], []);
    assert.ok(String(result["nextStep"]).includes("call get_quote NOW"));
  });
});
