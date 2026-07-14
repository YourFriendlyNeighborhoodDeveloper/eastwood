"""
Calendly scheduling provider.

Availability is retrieved from the real API before times are offered; the
selected slot is booked through the Scheduling API; every failure raises
CalendlyUnavailableError so callers fall back honestly — no booking is ever
reported confirmed when Calendly did not confirm it.
"""
from __future__ import annotations

import httpx
import pytest

from config.settings import Settings
from domain.scheduling import (
    CalendlySchedulingProvider,
    CalendlyUnavailableError,
    make_scheduling_provider,
)

EVENT_TYPE_URI = "https://api.calendly.com/event_types/ET-123"
SLOT_1 = "2026-07-16T14:00:00Z"
SLOT_2 = "2026-07-16T15:30:00Z"


def _provider(handler, *, event_type_uri: str = "") -> CalendlySchedulingProvider:
    return CalendlySchedulingProvider(
        access_token="tok-1",
        event_type_url="https://calendly.com/abraxastechnologies/30min",
        event_type_uri=event_type_uri,
        default_timezone="America/Detroit",
        transport=httpx.MockTransport(handler),
    )


def _api(requests_seen: list):
    """A fake Calendly API that records every request path in order."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request.url.path)
        if request.url.path == "/event_types/lookup":
            assert request.url.params["url"] == "https://calendly.com/abraxastechnologies/30min"
            return httpx.Response(200, json={"resource": {"uri": EVENT_TYPE_URI}})
        if request.url.path == "/event_type_available_times":
            assert request.url.params["event_type"] == EVENT_TYPE_URI
            return httpx.Response(200, json={"collection": [
                {"status": "available", "start_time": SLOT_1},
                {"status": "unavailable", "start_time": "2026-07-16T15:00:00Z"},
                {"status": "available", "start_time": SLOT_2},
            ]})
        if request.url.path == "/invitees":
            import json
            body = json.loads(request.content)
            assert body["event_type"] == EVENT_TYPE_URI
            assert body["invitee"]["email"], "no booking without an email"
            return httpx.Response(201, json={"resource": {
                "uri": f"{EVENT_TYPE_URI}/invitees/IN-1",
                "event": "https://api.calendly.com/scheduled_events/EV-1",
                "timezone": body["invitee"]["timezone"],
                "cancel_url": "https://calendly.com/cancellations/IN-1",
                "reschedule_url": "https://calendly.com/reschedulings/IN-1",
                "scheduled_event": {"start_time": body["start_time"]},
            }})
        return httpx.Response(404)

    return handler


# ── Availability ──────────────────────────────────────────────────────────────

async def test_availability_returns_only_available_slots_soonest_first():
    seen: list = []
    provider = _provider(_api(seen))

    times = await provider.available_times()

    assert times == [SLOT_1, SLOT_2], "unavailable slots are filtered out"
    assert seen == ["/event_types/lookup", "/event_type_available_times"]


async def test_event_type_lookup_is_cached_across_calls():
    seen: list = []
    provider = _provider(_api(seen))

    await provider.available_times()
    await provider.available_times()

    assert seen.count("/event_types/lookup") == 1, "the URL→URI lookup happens once"


async def test_configured_event_type_uri_skips_the_lookup():
    seen: list = []
    provider = _provider(_api(seen), event_type_uri=EVENT_TYPE_URI)

    await provider.available_times()

    assert "/event_types/lookup" not in seen


async def test_availability_failure_raises_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event_types/lookup":
            return httpx.Response(200, json={"resource": {"uri": EVENT_TYPE_URI}})
        return httpx.Response(500)

    with pytest.raises(CalendlyUnavailableError):
        await _provider(handler).available_times()


# ── Booking ───────────────────────────────────────────────────────────────────

async def test_selected_time_is_booked_and_identifiers_returned():
    seen: list = []
    provider = _provider(_api(seen))

    times = await provider.available_times()
    booking = await provider.create_booking(
        start_time=times[0],
        name="Dolores Vega",
        email="d.vega@example.com",
        timezone_name="America/Detroit",
        phone="+13135550123",
    )

    # Availability was retrieved BEFORE the booking call.
    assert seen.index("/event_type_available_times") < seen.index("/invitees")

    assert booking.status == "confirmed"
    assert booking.start_time == SLOT_1
    assert booking.event_uri == "https://api.calendly.com/scheduled_events/EV-1"
    assert booking.invitee_uri.endswith("/invitees/IN-1")
    assert booking.cancel_url and booking.reschedule_url
    assert booking.timezone == "America/Detroit"


async def test_booking_without_email_is_refused():
    provider = _provider(_api([]))
    with pytest.raises(CalendlyUnavailableError):
        await provider.create_booking(start_time=SLOT_1, name="Dolores", email="")


async def test_booking_failure_raises_never_confirms():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/event_types/lookup":
            return httpx.Response(200, json={"resource": {"uri": EVENT_TYPE_URI}})
        return httpx.Response(503)

    with pytest.raises(CalendlyUnavailableError):
        await _provider(handler).create_booking(
            start_time=SLOT_1, name="Dolores", email="d.vega@example.com"
        )


# ── Settings wiring ───────────────────────────────────────────────────────────

def test_provider_disabled_or_unconfigured_yields_none():
    assert make_scheduling_provider(Settings(calendly_enabled=False)) is None
    assert make_scheduling_provider(
        Settings(calendly_enabled=True, calendly_access_token="")
    ) is None


def test_provider_built_from_settings():
    provider = make_scheduling_provider(Settings(
        calendly_enabled=True,
        calendly_access_token="tok",
        calendly_event_type_url="https://calendly.com/abraxastechnologies/30min",
    ))
    assert isinstance(provider, CalendlySchedulingProvider)


def test_ryans_notification_number_is_the_settings_default():
    # Spec: all agent notifications route to +13135550199, configured in one
    # place — settings — never scattered through business logic.
    assert Settings().agent_notification_phone_e164 == "+13135550199"
