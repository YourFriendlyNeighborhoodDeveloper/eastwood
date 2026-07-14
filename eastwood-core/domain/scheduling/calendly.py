"""
Calendly scheduling provider.

book_appointment does not hardcode scheduling logic: it talks to a
SchedulingProvider, and this is the Calendly implementation. It resolves Ryan's
event type from the configured public URL (cached), retrieves real available
times before any time is offered to a prospect, and creates the invitee booking
through Calendly's Scheduling API — the backend books the slot, it does not
merely recite the public link.

Failure is honest: any API error raises CalendlyUnavailableError. Callers keep
the collected profile, route the requested time to Ryan, record
calendly_status="failed", and never tell the prospect a booking is confirmed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

_API_BASE = "https://api.calendly.com"


class CalendlyUnavailableError(RuntimeError):
    """Calendly could not complete the request. Booking must fall back honestly."""


@dataclass
class CalendlyBooking:
    """A confirmed Calendly invitee booking — persisted with the appointment."""

    event_uri: str
    invitee_uri: str
    start_time: str
    timezone: str
    cancel_url: str | None
    reschedule_url: str | None
    status: str  # "confirmed"


class SchedulingProvider(Protocol):
    async def available_times(self, *, timezone_name: str | None = None, days: int = 7) -> list[str]: ...

    async def create_booking(
        self, *, start_time: str, name: str, email: str, timezone_name: str | None = None, phone: str | None = None
    ) -> CalendlyBooking: ...


class CalendlySchedulingProvider:
    def __init__(
        self,
        *,
        access_token: str,
        event_type_url: str,
        event_type_uri: str = "",
        default_timezone: str = "America/Detroit",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = access_token
        self._event_type_url = event_type_url
        self._event_type_uri = event_type_uri or None
        self._default_timezone = default_timezone
        self._timeout = timeout
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=_API_BASE,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
            timeout=self._timeout,
            transport=self._transport,
        )

    async def resolve_event_type(self) -> str:
        """The event type's API URI, looked up once from the public URL and cached."""
        if self._event_type_uri:
            return self._event_type_uri
        try:
            async with self._client() as client:
                resp = await client.get("/event_types/lookup", params={"url": self._event_type_url})
                resp.raise_for_status()
                uri = resp.json().get("resource", {}).get("uri")
        except Exception as exc:
            raise CalendlyUnavailableError(f"event type lookup failed: {exc}") from exc
        if not uri:
            raise CalendlyUnavailableError("event type lookup returned no uri")
        self._event_type_uri = uri
        return uri

    async def available_times(self, *, timezone_name: str | None = None, days: int = 7) -> list[str]:
        """Upcoming available start times (ISO 8601), soonest first.

        Always called before offering times to a prospect — offered slots are
        real Calendly availability, never invented.
        """
        event_type = await self.resolve_event_type()
        # Calendly caps the range at 7 days per request; start slightly in the
        # future so the first returned slot is actually bookable.
        start = datetime.now(timezone.utc) + timedelta(hours=1)
        end = start + timedelta(days=min(days, 7))
        params = {
            "event_type": event_type,
            "start_time": start.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "end_time": end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
        try:
            async with self._client() as client:
                resp = await client.get("/event_type_available_times", params=params)
                resp.raise_for_status()
                collection = resp.json().get("collection", [])
        except Exception as exc:
            raise CalendlyUnavailableError(f"availability lookup failed: {exc}") from exc

        return [
            slot["start_time"]
            for slot in collection
            if slot.get("status") == "available" and slot.get("start_time")
        ]

    async def create_booking(
        self,
        *,
        start_time: str,
        name: str,
        email: str,
        timezone_name: str | None = None,
        phone: str | None = None,
    ) -> CalendlyBooking:
        """Create the invitee booking for a selected available time."""
        if not email:
            raise CalendlyUnavailableError("invitee email is required to book")
        event_type = await self.resolve_event_type()
        tz = timezone_name or self._default_timezone
        payload: dict = {
            "event_type": event_type,
            "start_time": start_time,
            "invitee": {"name": name or "Prospect", "email": email, "timezone": tz},
        }
        if phone:
            payload["invitee"]["phone_number"] = phone
        try:
            async with self._client() as client:
                resp = await client.post("/invitees", json=payload)
                resp.raise_for_status()
                resource = resp.json().get("resource", {})
        except Exception as exc:
            raise CalendlyUnavailableError(f"invitee booking failed: {exc}") from exc

        invitee_uri = resource.get("uri")
        event_uri = resource.get("event")
        if not invitee_uri or not event_uri:
            raise CalendlyUnavailableError("booking response missing invitee or event uri")

        return CalendlyBooking(
            event_uri=event_uri,
            invitee_uri=invitee_uri,
            start_time=resource.get("scheduled_event", {}).get("start_time") or start_time,
            timezone=resource.get("timezone") or tz,
            cancel_url=resource.get("cancel_url"),
            reschedule_url=resource.get("reschedule_url"),
            status="confirmed",
        )


def make_scheduling_provider(settings) -> CalendlySchedulingProvider | None:
    """Provider from settings, or None when Calendly is disabled/unconfigured."""
    if not settings.calendly_enabled:
        return None
    if not settings.calendly_access_token or not settings.calendly_event_type_url:
        logger.warning("CALENDLY_ENABLED is set but token or event type URL is missing")
        return None
    return CalendlySchedulingProvider(
        access_token=settings.calendly_access_token,
        event_type_url=settings.calendly_event_type_url,
        event_type_uri=settings.calendly_event_type_uri,
        default_timezone=settings.calendly_default_timezone,
    )
