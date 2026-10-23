/**
 * Regression: LeadContextSchema must accept null on every optional field.
 *
 * eastwood-core's clients/ai_agent.py always includes keys like lastName,
 * triageBucket, age, score, etc. in the /sessions request body — an unset
 * Python field arrives as JSON null, not an absent key. Zod's .optional()
 * alone accepts undefined but rejects null, so any real lead missing (e.g.)
 * a last name or a triage bucket made session creation 500 outright — the
 * call never started. Found via a live test call against the running stack,
 * not a hypothetical: this is the exact shape ai_agent.py sends.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { describe, it } from "node:test";

import { LeadContextSchema } from "../types.js";

describe("LeadContextSchema null tolerance", () => {
  it("accepts the exact enriched_context shape ai_agent.py sends for a lead with minimal data", () => {
    const payload = {
      leadId: randomUUID(),
      tenantId: "default",
      phoneE164: "+13135550199",
      firstName: "Ryan",
      lastName: null,
      age: 68,
      state: "MI",
      productLine: "final_expense",
      consentStatus: null,
      score: 50,
      enrichment: null,
      triageBucket: null,
      acquisitionPlan: [],
    };

    const parsed = LeadContextSchema.parse(payload);
    assert.equal(parsed.lastName, null);
    assert.equal(parsed.triageBucket, null);
  });

  it("still works when every optional field is simply absent (undefined)", () => {
    const parsed = LeadContextSchema.parse({
      leadId: randomUUID(),
      tenantId: "default",
      phoneE164: "+13135550199",
    });
    assert.equal(parsed.firstName, undefined);
  });

  it("still rejects a genuinely malformed payload — this isn't a blanket loosening", () => {
    assert.throws(() =>
      LeadContextSchema.parse({
        leadId: "not-a-uuid",
        tenantId: "default",
        phoneE164: "+13135550199",
      })
    );
    assert.throws(() =>
      LeadContextSchema.parse({
        leadId: randomUUID(),
        tenantId: "default",
        // phoneE164 missing entirely
      })
    );
  });
});
