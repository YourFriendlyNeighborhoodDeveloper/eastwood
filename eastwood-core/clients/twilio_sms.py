from __future__ import annotations

from twilio.rest import Client

from config.settings import get_settings


class SmsClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
        self._from_number = settings.twilio_from_number

    def send(self, to: str, body: str) -> str:
        message = self._client.messages.create(
            body=body,
            from_=self._from_number,
            to=to,
        )
        return message.sid

    def make_call(self, to: str, twiml_url: str, status_callback_url: str | None = None) -> str:
        kwargs: dict = dict(to=to, from_=self._from_number, url=twiml_url)
        if status_callback_url:
            kwargs["status_callback"] = status_callback_url
            kwargs["status_callback_event"] = ["completed", "no-answer", "busy", "failed", "canceled"]
            kwargs["status_callback_method"] = "POST"
        call = self._client.calls.create(**kwargs)
        return call.sid

    def make_call_with_amd(self, to: str, twiml_url: str, amd_callback_url: str) -> str:
        """Outbound call with async answering machine detection.
        Twilio POSTs AnsweredBy to amd_callback_url without interrupting the call flow."""
        call = self._client.calls.create(
            to=to,
            from_=self._from_number,
            url=twiml_url,
            machine_detection="Enable",
            async_amd=True,
            async_amd_status_callback=amd_callback_url,
            async_amd_status_callback_method="POST",
        )
        return call.sid
