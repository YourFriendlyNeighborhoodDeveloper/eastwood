/**
 * Booking against the licensed agent's own calendar (core AGENT_AVAILABILITY).
 *
 * Proves:
 *   - when core says requires_email=false, a chosen slot books without
 *     collecting an email (Calendly still requires one)
 *   - only offered times can be booked
 *   - a 409 (slot just taken / day full) is never reported as booked; the
 *     model is told to fetch fresh availability
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

let toolHandlers: typeof import("../tools/toolHandlers.js").toolHandlers;

before(async () => {
  ({ toolHandlers } = await import("../tools/toolHandlers.js"));
});

const SLOTS = ["2026-12-01T17:00:00-05:00", "2026-12-01T17:30:00-05:00", "2026-12-01T18:00:00-05:00"];

function makeSession(): SessionState {
  return {
    lead: { leadId: "1f0e9b8a-0000-4000-8000-000000000002", tenantId: "default", phoneE164: "+15550001112", age: 70 } as unknown as LeadContext,
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
    userTranscripts: [],
    captureEvidence: {},
  } as unknown as SessionState;
}

function ctx(session: SessionState) {
  return {
    lead: session.lead,
    sessionId: "sess-local-cal",
    bridge: { publishEvent: async () => {} } as never,
    actor: { send: () => {} } as never,
    session,
  };
}

const calls: { url: string; body: unknown }[] = [];
let availability: Record<string, unknown> = {};
let bookingStatus = 201;

const originalFetch = globalThis.fetch;
globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(input);
  calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null });
  if (url.includes("/api/scheduling/availability")) {
    return new Response(JSON.stringify(availability), { status: 200 });
  }
  if (url.includes("/api/appointments")) {
    if (bookingStatus !== 201) {
      return new Response(JSON.stringify({ detail: { error: "slot_unavailable" } }), { status: bookingStatus });
    }
    return new Response(
      JSON.stringify({ appointment_id: "appt-1", date: "2026-12-01", time: "17:00", calendly_status: "not_requested" }),
      { status: 201 },
    );
  }
  return new Response("{}", { status: 200 });
}) as typeof fetch;

after(() => {
  globalThis.fetch = originalFetch;
});

beforeEach(() => {
  calls.length = 0;
  bookingStatus = 201;
  availability = { calendly_enabled: false, calendar: "local", requires_email: false, times: SLOTS, timezone: "America/Detroit" };
});

describe("booking on the agent's own calendar", () => {
  it("books an offered slot without asking for an email", async () => {
    const session = makeSession();
    const slots: any = await toolHandlers["get_booking_slots"]!({}, ctx(session));
    assert.deepEqual(slots.times, SLOTS);
    assert.match(slots.note, /No email is needed/);

    const result: any = await toolHandlers["book_appointment"]!({ slotStartTime: SLOTS[1] }, ctx(session));
    assert.equal(result.booked, true);
    assert.equal(result.needsEmail, undefined);
    const posted = calls.find((c) => c.url.includes("/api/appointments"))!.body as Record<string, unknown>;
    assert.equal(posted["start_time"], SLOTS[1]);
  });

  it("still requires an email when the calendar does (Calendly)", async () => {
    availability = { calendly_enabled: true, times: SLOTS, timezone: "America/Detroit" };
    const session = makeSession();
    await toolHandlers["get_booking_slots"]!({}, ctx(session));
    const result: any = await toolHandlers["book_appointment"]!({ slotStartTime: SLOTS[0] }, ctx(session));
    assert.equal(result.booked, false);
    assert.equal(result.needsEmail, true);
    assert.equal(calls.filter((c) => c.url.includes("/api/appointments")).length, 0);
  });

  it("refuses a time that was never offered", async () => {
    const session = makeSession();
    await toolHandlers["get_booking_slots"]!({}, ctx(session));
    const result: any = await toolHandlers["book_appointment"]!({ slotStartTime: "2026-12-01T19:00:00-05:00" }, ctx(session));
    assert.equal(result.booked, false);
    assert.equal(calls.filter((c) => c.url.includes("/api/appointments")).length, 0);
  });

  it("a slot taken a moment ago (409) is never reported as booked", async () => {
    const session = makeSession();
    await toolHandlers["get_booking_slots"]!({}, ctx(session));
    bookingStatus = 409;
    const result: any = await toolHandlers["book_appointment"]!({ slotStartTime: SLOTS[0] }, ctx(session));
    assert.equal(result.booked, false);
    assert.equal(result.slotTaken, true);
    assert.equal(session.appointmentBooked, false);
    assert.deepEqual(session.offeredSlots, [], "stale offers are cleared");
  });
});
