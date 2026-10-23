/**
 * Needs discovery gating, deterministic face-amount selection, and the handoff
 * profile. Imogen must not price before she knows what she is pricing and for
 * whom, must not invent a coverage amount, and must hand Ryan a brief built from
 * captured facts — never a model-authored summary.
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] ??= "http://eastwood-core:8080";
process.env["APP_ENV"] = "production";

import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { before, describe, it } from "node:test";

import type { LeadContext, SessionState } from "../types.js";
import { UNKNOWN } from "../types.js";
import {
  CAPTURE,
  buildProspectProfile,
  minimumDiscoveryCaptured,
  quoteUnlocked,
  selectFaceAmount,
} from "../discovery/prospectProfile.js";
import { deriveAllowedTools } from "../tools/toolGating.js";
import type { KafkaBridge } from "../bridge/kafkaBridge.js";

// toolHandlers pulls in observability, which calls loadConfig() at import time.
// Load it dynamically after the env above is in place (imports otherwise hoist).
let toolHandlers: typeof import("../tools/toolHandlers.js").toolHandlers;

function lead(overrides: Partial<LeadContext> = {}): LeadContext {
  return {
    leadId: randomUUID(),
    tenantId: "default",
    phoneE164: "+13135550123",
    firstName: "Dolores",
    lastName: "Vega",
    age: 68,
    state: "MI",
    productLine: "final_expense",
    consentStatus: "express_written",
    acquisitionPlan: [],
    ...overrides,
  };
}

function session(captured: Record<string, unknown> = {}, extra: Partial<SessionState> = {}): SessionState {
  const l = extra.lead ?? lead();
  return {
    lead: l,
    quote: null,
    appointmentBooked: false,
    captured,
    objections: [],
    appointment: null,
    dncCaptured: false,
    callEndedPublished: false,
    userTranscripts: [],
    captureEvidence: {},
    ...extra,
    lead: l,
  } as SessionState;
}

const FULL_DISCOVERY = {
  [CAPTURE.coveragePurpose]: "burial",
  [CAPTURE.beneficiaryRelationship]: "spouse",
  [CAPTURE.desiredCoverageAmount]: 10_000,
  [CAPTURE.ratingSex]: "female",
};

describe("needs discovery gating", () => {
  it("keeps get_quote locked without purpose + beneficiary + amount/budget", () => {
    const l = lead(); // Tier-1 health agenda already empty
    // Nothing captured.
    assert.equal(minimumDiscoveryCaptured(session({})), false);
    assert.ok(!deriveAllowedTools(l, session({})).includes("get_quote"));

    // Purpose only.
    assert.ok(!deriveAllowedTools(l, session({ [CAPTURE.coveragePurpose]: "burial" })).includes("get_quote"));

    // Purpose + beneficiary, still no amount or budget.
    assert.ok(
      !deriveAllowedTools(
        l,
        session({ [CAPTURE.coveragePurpose]: "burial", [CAPTURE.beneficiaryRelationship]: "spouse" }),
      ).includes("get_quote"),
      "amount or budget is still required",
    );
  });

  it("unlocks get_quote once the minimum profile is present", () => {
    const l = lead();
    assert.ok(quoteUnlocked(l, session(FULL_DISCOVERY)));
    assert.ok(deriveAllowedTools(l, session(FULL_DISCOVERY)).includes("get_quote"));
  });

  it("keeps get_quote locked until the rating-sex question was asked", () => {
    const l = lead();
    const { [CAPTURE.ratingSex]: _omit, ...withoutRatingSex } = FULL_DISCOVERY;
    assert.ok(
      !deriveAllowedTools(l, session(withoutRatingSex)).includes("get_quote"),
      "rating sex must be asked before quoting",
    );
  });

  it("accepts a declined rating sex recorded as unknown — asked, not guessed", () => {
    const l = lead();
    const captured = { ...FULL_DISCOVERY, [CAPTURE.ratingSex]: "unknown" };
    assert.ok(
      deriveAllowedTools(l, session(captured)).includes("get_quote"),
      "a declined answer still satisfies the gate",
    );
  });

  it("accepts a monthly budget in place of a coverage amount", () => {
    const captured = {
      [CAPTURE.coveragePurpose]: "family_protection",
      [CAPTURE.beneficiaryName]: "Maria",
      [CAPTURE.monthlyBudget]: 45,
    };
    assert.ok(minimumDiscoveryCaptured(session(captured)));
  });

  it("keeps get_quote locked until Tier-1 health is also complete", () => {
    // Health agenda still has an outstanding item.
    const l = lead({ acquisitionPlan: [{ field: "tobacco", priority: 2, question: "Tobacco?", captureKey: "tobacco" }] });
    assert.ok(!deriveAllowedTools(l, session(FULL_DISCOVERY)).includes("get_quote"), "health screening not done");
  });

  it("keeps book_appointment locked while quoting is possible but not yet done", () => {
    const l = lead();
    const tools = deriveAllowedTools(l, session(FULL_DISCOVERY));
    assert.ok(tools.includes("get_quote"), "precondition: quoting is possible");
    assert.ok(!tools.includes("book_appointment"), "booking may not replace quoting");
    assert.ok(!tools.includes("get_booking_slots"));
  });

  it("unlocks honest booking after a quote-service failure", () => {
    const l = lead();
    const s = session(FULL_DISCOVERY, { quoteFailureReason: "quote_service_unavailable" } as any);
    const tools = deriveAllowedTools(l, s);
    assert.ok(tools.includes("book_appointment"), "manual-quote booking must be possible");
    assert.ok(tools.includes("get_booking_slots"));
  });

  it("unlocks booking when the prospect explicitly asks for only an appointment", () => {
    const l = lead();
    const s = session({ appointment_only_request: "yes" });
    assert.ok(deriveAllowedTools(l, s).includes("book_appointment"));
  });
});

describe("face-amount selection", () => {
  it("uses the prospect's requested amount and records prospect_requested", () => {
    const sel = selectFaceAmount(session({ [CAPTURE.desiredCoverageAmount]: 10_000 }), 68);
    assert.deepEqual(sel, { faceValue: 10_000, reason: "prospect_requested" });
  });

  it("sizes to budget and records budget_fit", () => {
    const sel = selectFaceAmount(session({ [CAPTURE.monthlyBudget]: 40 }), 68);
    // At 68 (~$5.50 per $1k/mo), $40 affords the $5k-$? band deterministically.
    assert.equal(sel.reason, "budget_fit");
    assert.ok(sel.faceValue >= 5_000 && sel.faceValue <= 30_000);
    assert.ok((sel.faceValue / 1000) * 5.5 <= 40, "the sized face must fit the budget");
  });

  it("records needs_estimate for a final-expense purpose with no amount or budget", () => {
    const sel = selectFaceAmount(session({ [CAPTURE.coveragePurpose]: "burial" }), 68);
    assert.equal(sel.reason, "needs_estimate");
  });

  it("records agent_default when there is no stated basis at all", () => {
    const sel = selectFaceAmount(session({}), 68);
    assert.equal(sel.reason, "agent_default");
  });
});

describe("get_quote face-amount enforcement (production)", () => {
  before(async () => {
    ({ toolHandlers } = await import("../tools/toolHandlers.js"));
  });

  function ctx(s: SessionState) {
    const events: any[] = [];
    return {
      events,
      ctx: {
        lead: s.lead,
        sessionId: "sess-1",
        bridge: { publishEvent: async (e: any) => { events.push(e); } } as unknown as KafkaBridge,
        actor: { send: () => {} } as any,
        session: s,
      },
    };
  }

  function stubQuote(faceSeen: { value?: number }) {
    const original = globalThis.fetch;
    globalThis.fetch = (async (_url: any, init?: any) => {
      const body = init?.body ? JSON.parse(init.body) : {};
      faceSeen.value = body.face_value;
      return {
        ok: true,
        status: 200,
        json: async () => ({
          quote_source: "estimate",
          quote_type: "estimated",
          rate_source: "formula_adapter",
          carrier_rate: false,
          bindable: false,
          best: { carrier: "Mutual of Omaha", monthly_premium: 52.3, rate_tier: "level", plan_name: null, source: "estimate" },
          all_options: [],
          errors: [],
        }),
      };
    }) as typeof fetch;
    return () => { globalThis.fetch = original; };
  }

  it("allows a prospect-requested $10,000 quote and prices exactly that", async () => {
    const s = session({ [CAPTURE.desiredCoverageAmount]: 10_000, ...FULL_DISCOVERY });
    const { ctx: c } = ctx(s);
    const faceSeen: { value?: number } = {};
    const restore = stubQuote(faceSeen);
    try {
      const result: any = await toolHandlers.get_quote!({ productLine: "final_expense", age: 68, state: "MI" }, c);
      assert.equal(result.error, undefined, "a requested amount must quote even in production");
      assert.equal(result.faceAmountReason, "prospect_requested");
      assert.equal(faceSeen.value, 10_000, "core is asked to price the requested amount");
      assert.equal(s.quote?.faceValue, 10_000);
    } finally {
      restore();
    }
  });

  it("rejects an arbitrary default $10,000 quote in production", async () => {
    const s = session({}); // no purpose, amount, or budget → agent_default
    const { ctx: c } = ctx(s);
    const faceSeen: { value?: number } = {};
    const restore = stubQuote(faceSeen);
    try {
      const result: any = await toolHandlers.get_quote!({ productLine: "final_expense", age: 68, state: "MI" }, c);
      assert.ok(result.error, "an unfounded default must be refused");
      assert.match(result.error, /pricing the right amount/);
      assert.equal(faceSeen.value, undefined, "core must not be called for an unfounded quote");
      assert.equal(s.quote, null, "no quote is recorded");
    } finally {
      restore();
    }
  });
});

describe("book_appointment idempotency", () => {
  before(async () => {
    ({ toolHandlers } = await import("../tools/toolHandlers.js"));
  });

  it("does not POST or re-notify on a repeated booking", async () => {
    const s = session(FULL_DISCOVERY);
    let posts = 0;
    const original = globalThis.fetch;
    globalThis.fetch = (async () => {
      posts += 1;
      return { ok: true, status: 200, json: async () => ({ appointment_id: "appt-xyz" }) };
    }) as typeof fetch;

    const ctx = {
      lead: s.lead,
      sessionId: "sess-1",
      bridge: { publishEvent: async () => {} } as unknown as KafkaBridge,
      actor: { send: () => {} } as any,
      session: s,
    };
    try {
      const first: any = await toolHandlers.book_appointment!({ preferredDate: "2026-07-15", preferredTime: "10:00" }, ctx);
      const second: any = await toolHandlers.book_appointment!({ preferredDate: "2026-07-15", preferredTime: "10:00" }, ctx);

      assert.equal(first.booked, true);
      assert.equal(second.booked, true);
      assert.equal(second.appointmentId, first.appointmentId, "same appointment returned");
      assert.equal(posts, 1, "the second booking must not POST again");
    } finally {
      globalThis.fetch = original;
    }
  });
});

describe("prospect profile assembly", () => {
  it("contains every captured field", () => {
    const captured = {
      [CAPTURE.coveragePurpose]: "burial",
      [CAPTURE.beneficiaryName]: "Robert Vega",
      [CAPTURE.beneficiaryRelationship]: "husband",
      [CAPTURE.desiredCoverageAmount]: 15_000,
      [CAPTURE.monthlyBudget]: 60,
      [CAPTURE.existingCoverage]: "yes",
      [CAPTURE.existingCoverageAmount]: 5_000,
      [CAPTURE.motivation]: "want to cover my funeral so the kids don't have to",
      [CAPTURE.conditions]: [{ code: "diabetes", raw_text: "diabetes" }],
      [CAPTURE.medications]: "metformin",
      [CAPTURE.tobacco]: "none",
    };
    const s = session(captured);
    s.quote = {
      productLine: "final_expense",
      faceValue: 15_000,
      monthlyPremium: 62.5,
      carrier: "Mutual of Omaha",
      quoteType: "carrier",
      rateSource: "rate_table",
      carrierRate: true,
      bindable: false,
      faceAmountReason: "prospect_requested",
    };
    s.objections.push({ objectionType: "price", text: "that feels a bit steep" });

    const profile = buildProspectProfile(s, { appointmentTime: "2026-07-15 10:00 ET", followUpNotes: "prefers mornings" });

    assert.equal(profile.full_name, "Dolores Vega");
    assert.equal(profile.phone_e164, "+13135550123");
    assert.equal(profile.coverage_purpose, "burial");
    assert.equal(profile.beneficiary_name, "Robert Vega");
    assert.equal(profile.beneficiary_relationship, "husband");
    assert.equal(profile.desired_coverage_amount, 15_000);
    assert.equal(profile.monthly_budget, 60);
    assert.equal(profile.existing_coverage, "yes");
    assert.equal(profile.existing_coverage_amount, 5_000);
    assert.match(profile.motivation_summary, /cover my funeral/);
    assert.equal(profile.tobacco_status, "none");
    assert.equal(profile.quote_amount, 15_000);
    assert.equal(profile.quoted_monthly_premium, 62.5);
    assert.equal(profile.quote_type, "carrier");
    assert.equal(profile.face_amount_reason, "prospect_requested");
    assert.equal(profile.carrier_name, "Mutual of Omaha");
    assert.equal(profile.appointment_time, "2026-07-15 10:00 ET");
    assert.equal(profile.follow_up_notes, "prefers mornings");
    assert.deepEqual(profile.objections, [{ objectionType: "price", text: "that feels a bit steep" }]);
    assert.deepEqual(profile.risk_flags, ["condition: diabetes"]);
    assert.notEqual(profile.transcript_summary, UNKNOWN);
  });

  it("marks missing fields unknown and never invents them", () => {
    const profile = buildProspectProfile(session({}), { appointmentTime: "2026-07-15 10:00 ET" });

    assert.equal(profile.coverage_purpose, UNKNOWN);
    assert.equal(profile.beneficiary_name, UNKNOWN);
    assert.equal(profile.beneficiary_relationship, UNKNOWN);
    assert.equal(profile.existing_coverage, UNKNOWN);
    assert.equal(profile.motivation_summary, UNKNOWN);
    assert.equal(profile.desired_coverage_amount, null);
    assert.equal(profile.monthly_budget, null);
    assert.equal(profile.follow_up_notes, UNKNOWN);
    // No quote presented → no carrier, no premium.
    assert.equal(profile.carrier_name, null);
    assert.equal(profile.quoted_monthly_premium, null);
    assert.deepEqual(profile.risk_flags, []);
  });

  it("does not name a carrier for an estimate", () => {
    const s = session({});
    s.quote = {
      productLine: "final_expense",
      faceValue: 10_000,
      monthlyPremium: 52.3,
      carrier: "Eastwood Preferred",
      quoteType: "estimated",
      rateSource: "formula_adapter",
      carrierRate: false,
      bindable: false,
      faceAmountReason: "needs_estimate",
    };
    const profile = buildProspectProfile(s, { appointmentTime: "x" });
    assert.equal(profile.carrier_name, null, "an estimate is not attributed to a carrier");
    assert.equal(profile.quote_type, "estimated");
  });
});
