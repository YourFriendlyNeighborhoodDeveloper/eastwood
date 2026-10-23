/**
 * Internal service authentication against eastwood-core.
 *
 * Proves:
 *   - every core /api call carries `Authorization: Bearer <EASTWOOD_API_KEY>`,
 *     attached centrally by coreClient — no handler builds its own auth
 *   - a 401/403 from core surfaces as an honest tool failure: no premium is
 *     invented and no booking is reported as successful
 */

process.env["NODE_ENV"] = "production";
process.env["LOG_LEVEL"] = "silent";
process.env["AGENT_DATABASE_URL"] ??= "postgresql://test:test@localhost:5432/test";
process.env["KAFKA_BROKERS"] ??= "localhost:9092";
process.env["EASTWOOD_CORE_BASE_URL"] = "http://eastwood-core:8080";
process.env["EASTWOOD_API_KEY"] = "agent-test-key";

import assert from "node:assert/strict";
import { after, before, beforeEach, describe, it } from "node:test";

import type { LeadContext, SessionState } from "../types.js";

// Dynamic imports: the modules read config at load time, so the environment
// above must be in place first (static imports are hoisted past it).
let coreFetch: typeof import("../coreClient.js").coreFetch;
let toolHandlers: typeof import("../tools/toolHandlers.js").toolHandlers;

before(async () => {
  ({ coreFetch } = await import("../coreClient.js"));
  ({ toolHandlers } = await import("../tools/toolHandlers.js"));
});

const LEAD_ID = "1f0e9b8a-0000-4000-8000-000000000001";

function makeLead(): LeadContext {
  return {
    leadId: LEAD_ID,
    tenantId: "default",
    phoneE164: "+15550001111",
    age: 67,
  } as unknown as LeadContext;
}

function makeSession(): SessionState {
  return {
    lead: makeLead(),
    quote: null,
    quoteOptions: null,
    selectedQuoteOption: null,
    affordabilityConfirmed: null,
    quoteFailureReason: null,
    offeredSlots: [],
    calendly: null,
    appointmentBooked: false,
    captured: { desired_coverage_amount: 15000 },
    objections: [],
    appointment: null,
    dncCaptured: false,
    callEndedPublished: false,
    userTranscripts: [],
    captureEvidence: {},
  } as unknown as SessionState;
}

function makeCtx(session: SessionState) {
  return {
    lead: session.lead,
    sessionId: "sess-auth-test",
    bridge: { publishEvent: async () => {} } as never,
    actor: { send: () => {} } as never,
    session,
  };
}

type RecordedCall = { url: string; headers: Headers };

const recorded: RecordedCall[] = [];
let nextResponse: () => Response = () => new Response("{}", { status: 200 });

const originalFetch = globalThis.fetch;
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  recorded.push({ url: String(input), headers: new Headers(init?.headers) });
  return nextResponse();
}) as typeof fetch;

after(() => {
  globalThis.fetch = originalFetch;
});

beforeEach(() => {
  recorded.length = 0;
  nextResponse = () => new Response("{}", { status: 200 });
});

describe("core service authentication", () => {
  it("coreFetch attaches the Bearer key to every core call", async () => {
    await coreFetch("/api/quote", { method: "POST", body: "{}" });
    assert.equal(recorded.length, 1);
    assert.equal(recorded[0]!.url, "http://eastwood-core:8080/api/quote");
    assert.equal(recorded[0]!.headers.get("authorization"), "Bearer agent-test-key");
  });

  it("tool handlers reach core through the authenticated client", async () => {
    const session = makeSession();
    nextResponse = () =>
      new Response(JSON.stringify({ calendly_enabled: false, times: [] }), { status: 200 });
    await toolHandlers["get_booking_slots"]!({ timezone: "America/Detroit" }, makeCtx(session));

    assert.equal(recorded.length, 1);
    assert.ok(recorded[0]!.url.includes("/api/scheduling/availability"));
    assert.ok(recorded[0]!.url.includes("timezone=America%2FDetroit"));
    assert.equal(recorded[0]!.headers.get("authorization"), "Bearer agent-test-key");
  });

  it("a rejected quote call fails honestly — no premium is fabricated", async () => {
    const session = makeSession();
    nextResponse = () => new Response('{"detail":"forbidden"}', { status: 403 });

    const result = await toolHandlers["get_quote"]!(
      { productLine: "final_expense", age: 67, state: "MI" },
      makeCtx(session)
    );

    assert.equal(recorded[0]!.headers.get("authorization"), "Bearer agent-test-key");
    assert.ok(result["error"], "expected an error result");
    assert.equal(result["quoteFailed"], true);
    assert.equal(result["monthlyPremium"], undefined);
    assert.equal(session.quote, null);
    assert.equal(session.quoteFailureReason, "quote_service_unavailable");
  });

  it("a rejected booking call is never reported as booked", async () => {
    const session = makeSession();
    nextResponse = () => new Response('{"detail":"authentication required"}', { status: 401 });

    const result = await toolHandlers["book_appointment"]!(
      { preferredDate: "2026-07-20", preferredTime: "10:00" },
      makeCtx(session)
    );

    assert.equal(recorded[0]!.headers.get("authorization"), "Bearer agent-test-key");
    assert.equal(result["booked"], false);
    assert.ok(result["error"], "expected an error result");
    assert.equal(session.appointmentBooked, false);
    assert.equal(session.appointment, null);
  });
});
