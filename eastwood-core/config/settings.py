from __future__ import annotations

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://eastwood:eastwood@localhost:5432/eastwood"
    kafka_brokers: str = "localhost:9092"

    topic_leads_raw: str = "leads.raw"
    topic_leads_normalized: str = "leads.normalized"
    topic_leads_scored: str = "leads.scored"
    topic_leads_routed: str = "leads.routed"
    topic_agent_events: str = "agent.events"    # legacy catch-all; kept for backward compat
    topic_consent_events: str = "consent.events"
    topic_dlq: str = "dlq.events"

    # Discrete call-event topics (replacing the agent.events soup)
    topic_calls_field_captured: str = "calls.field_captured"
    topic_calls_quote_requested: str = "calls.quote_requested"
    topic_calls_quote_ready: str = "calls.quote_ready"
    topic_calls_quote_presented: str = "calls.quote_presented"
    topic_calls_objection_captured: str = "calls.objection_captured"
    topic_calls_consent_captured: str = "calls.consent_captured"
    topic_calls_dnc_requested: str = "calls.dnc_requested"
    topic_calls_live_transfer_requested: str = "calls.live_transfer_requested"
    topic_calls_ended: str = "calls.ended"

    redis_url: str = "redis://localhost:6379"

    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    facebook_app_secret: str = ""
    facebook_verify_token: str = ""

    ai_agent_base_url: str = "http://localhost:8081"

    openai_api_key: str = ""
    openfec_api_key: str = ""

    # Deployment environment. "production" enables the compliance boot guard.
    app_env: str = "development"

    enforce_tcpa: bool = True

    # Skips the 8am-9pm local-time TCPA calling window. Local testing only —
    # assert_production_safety() refuses to boot with this set in production.
    # domain.tcpa reads the BYPASS_TCPA environment variable directly; this
    # field exists so the guard can see the same value.
    bypass_tcpa: bool = False

    # National DNC Registry scrub. The provider is looked up by name in
    # domain.compliance.dnc_scrub so no vendor is hardcoded into the dial path.
    # "local_csv" is a blocklist-only stand-in: it can prove a number is on the
    # registry, never that it is clear, so on its own it dials nobody.
    dnc_scrub_provider: str = "local_csv"
    national_dnc_blocklist_path: str = "data/private/national_dnc_blocked.local.csv"

    score_hot_threshold: int = 75
    score_warm_threshold: int = 50

    default_tenant_id: str = "default"

    # Public-facing base URL (e.g. https://your-ngrok-subdomain.ngrok.io).
    # Required for Twilio to reach the TwiML and WebSocket endpoints.
    # Leave blank to skip outbound call placement (session is still created).
    public_base_url: str = ""

    # Live handoff: Ryan's mobile number receives hot-transfer SMS briefs.
    # Set RYAN_PHONE_NUMBER=+1XXXXXXXXXX to enable.
    ryan_phone_number: str = ""

    # Autonomous dialer controls
    max_concurrent_calls: int = 3        # hard cap on simultaneous outbound calls
    dial_max_attempts: int = 5           # total attempts per lead before giving up
    voicemail_message: str = (
        "Hi, this is Alex from Eastwood Financial calling about the life insurance "
        "options you inquired about. I'll try you again soon, or feel free to call "
        "us back. Have a great day!"
    )

    # ── Real-time quoting ────────────────────────────────────────────────────
    cms_ma_dataset_id: str = "pjlv-rqvd"
    cms_ms_dataset_id: str = ""
    cms_target_carriers: str = "aetna,humana,cvs"
    quote_timeout_seconds: float = 6.0

    # Comma-separated list of states where carriers are configured and auto-dial is active.
    # Set ACTIVE_STATES=MI,TX,FL,CA etc. to expand coverage.
    active_states: str = "MI"

    def active_states_list(self) -> list[str]:
        return [s.strip().upper() for s in self.active_states.split(",") if s.strip()]

    def consumed_topics(self) -> list[str]:
        """Every topic eastwood-core subscribes to.

        Must cover every topic the ai-agent publishes to (see kafkaBridge.ts
        topicForEvent), or those events are produced and silently dropped.
        """
        return [
            self.topic_leads_raw,
            self.topic_leads_normalized,
            # legacy catch-all (kept while traffic migrates to discrete topics)
            self.topic_agent_events,
            # discrete call-event topics
            self.topic_calls_quote_requested,
            self.topic_calls_quote_presented,
            self.topic_calls_field_captured,
            self.topic_calls_objection_captured,
            self.topic_calls_consent_captured,
            self.topic_calls_dnc_requested,
            self.topic_calls_live_transfer_requested,
            self.topic_calls_ended,
        ]

    @field_validator("score_hot_threshold", "score_warm_threshold")
    @classmethod
    def thresholds_in_range(cls, v: int) -> int:
        if not 0 <= v <= 100:
            raise ValueError("threshold must be 0-100")
        return v


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


class UnsafeProductionConfigError(RuntimeError):
    """Raised at startup when a development-only escape hatch is set in production."""


def assert_production_safety(settings: Settings | None = None) -> None:
    """Refuse to boot a production deployment with compliance checks disabled.

    BYPASS_TCPA short-circuits the 8am-9pm local-time calling window. Dialing
    outside that window is a per-call TCPA violation, so production must never
    start with it set.
    """
    s = settings or get_settings()
    if s.app_env.strip().lower() == "production" and s.bypass_tcpa:
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with BYPASS_TCPA=true. "
            "This disables the TCPA calling-window check for live outbound "
            "dialing. Unset BYPASS_TCPA, or set APP_ENV to a non-production value."
        )
