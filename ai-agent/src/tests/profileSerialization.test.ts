/**
 * The handoff profile renders captured values as readable text — Ryan's brief
 * must show "height: 5'10, weight: 180", never "[object Object]". A captured
 * object, array, or scalar all flatten to facts; empty values stay "unknown".
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { CAPTURE, buildProspectProfile } from "../discovery/prospectProfile.js";
import type { LeadContext, SessionState } from "../types.js";

function makeSession(captured: Record<string, unknown>): SessionState {
  return {
    lead: {
      leadId: "1f0e9b8a-0000-4000-8000-000000000001",
      tenantId: "default",
      phoneE164: "+15550001111",
      firstName: "Ryan",
      lastName: "TestLead",
      age: 66,
      state: "MI",
    } as unknown as LeadContext,
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

describe("profile serialization", () => {
  it("flattens a captured object to readable key/value text", () => {
    const session = makeSession({
      [CAPTURE.heightWeight]: { height: "5'10", weight: 180 },
    });
    const profile = buildProspectProfile(session, { appointmentTime: "" });
    assert.equal(profile.height_weight, "height: 5'10, weight: 180");
    assert.ok(!JSON.stringify(profile).includes("[object Object]"));
  });

  it("keeps plain string captures verbatim", () => {
    const session = makeSession({ [CAPTURE.heightWeight]: "5'10\" 180 lbs" });
    const profile = buildProspectProfile(session, { appointmentTime: "" });
    assert.equal(profile.height_weight, "5'10\" 180 lbs");
  });

  it("captured budget lands on monthly_budget for the outcome brief", () => {
    const session = makeSession({ [CAPTURE.monthlyBudget]: "45" });
    const profile = buildProspectProfile(session, { appointmentTime: "" });
    assert.equal(profile.monthly_budget, 45);
  });

  it("an empty object stays unknown, never invented", () => {
    const session = makeSession({ [CAPTURE.heightWeight]: {} });
    const profile = buildProspectProfile(session, { appointmentTime: "" });
    assert.equal(profile.height_weight, "unknown");
  });
});
