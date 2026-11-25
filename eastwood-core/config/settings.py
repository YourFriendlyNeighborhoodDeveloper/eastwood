from __future__ import annotations

from urllib.parse import urlparse

from pydantic import field_validator, model_validator
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
    # "ftc_registry" is the official downloaded-dataset provider.
    dnc_scrub_provider: str = "local_csv"
    national_dnc_blocklist_path: str = "data/private/national_dnc_blocked.local.csv"

    # Comma-separated E.164 numbers the operator personally owns and consents to
    # being called for end-to-end testing. These scrub as "exempt" without hitting
    # a provider. This is NOT a bypass: every other number still fails closed.
    # Must be empty in production (enforced by the boot guard).
    dnc_test_allowlist: str = ""

    # ── Official FTC National DNC Registry (provider "ftc_registry") ───────────
    # Subscription Account Number and login. Secrets — never committed, never
    # logged; only a fingerprint is persisted.
    dnc_san: str = ""
    dnc_registry_username: str = ""
    dnc_registry_password: str = ""
    # Comma-separated subscribed area codes, e.g. "313,248,586".
    dnc_area_codes: str = ""
    # Downloaded Registry dataset lives here — under data/private, gitignored.
    dnc_data_directory: str = "data/private/dnc_registry"
    # A dataset older than this many days can no longer clear a number.
    dnc_max_data_age_days: int = 31
    # Operator-configured automated-download endpoint (documented FTC interface).
    dnc_registry_download_url: str = ""

    score_hot_threshold: int = 75
    score_warm_threshold: int = 50

    default_tenant_id: str = "default"

    # Public-facing base URL (e.g. https://your-ngrok-subdomain.ngrok.io).
    # Required for Twilio to reach the TwiML and WebSocket endpoints.
    # Leave blank to skip outbound call placement (session is still created).
    public_base_url: str = ""

    # Twilio webhook authentication. Every /twiml/* and /twilio/* request must
    # carry a valid X-Twilio-Signature computed over the public URL. Disabling
    # this is a development-only affordance: production refuses to boot with it
    # off, and a disabled deployment logs a loud warning at startup.
    twilio_signature_validation_enabled: bool = True

    # Bearer key for every operational /api/* route. Secret — never committed,
    # never logged, never echoed in a response. Empty means no credential can
    # succeed (fail closed); production refuses to boot without one.
    eastwood_api_key: str = ""

    # Live handoff: Ryan's mobile number receives hot-transfer SMS briefs.
    # Set RYAN_PHONE_NUMBER=+1XXXXXXXXXX to enable.
    ryan_phone_number: str = ""

    # Agent notifications: Ryan receives every call-outcome SMS and every
    # appointment brief here. Business logic must read this setting — the number
    # is configured in exactly one place. If cleared, sends are skipped with a
    # warning and the full profile remains retrievable from the DB.
    #
    # Must default to "" — AppointmentNotificationService's fail-soft warning
    # path only fires when this is falsy. A non-empty default here is silently
    # unrecoverable: bookings "succeed" and send real SMS to whatever this
    # value is instead of to Ryan, with no error anywhere. (Previously
    # hardcoded to a stray number; see docs/go-live-checklist.md §6.)
    agent_notification_phone_e164: str = ""

    # ── Calendly scheduling ──────────────────────────────────────────────────
    # When enabled, appointments are booked against Ryan's real Calendly event
    # type: availability is fetched before offering times, and the selected slot
    # is created through the Scheduling API. When disabled or unavailable the
    # profile is still preserved and the requested time reaches Ryan — but the
    # prospect is never told a Calendly booking is confirmed when it is not.
    calendly_enabled: bool = False
    calendly_access_token: str = ""
    calendly_event_type_url: str = "https://calendly.com/abraxastechnologies/30min"
    # Resolved API URI for the event type (cached; looked up from the URL when blank).
    calendly_event_type_uri: str = ""
    calendly_default_timezone: str = "America/Detroit"

    # ── Quote option limits ──────────────────────────────────────────────────
    # Product face-amount bounds for the three-option (C/B/A) quote builder.
    # Option faces are always clamped inside these limits.
    quote_min_face_dollars: int = 5_000
    quote_max_face_dollars: int = 50_000

    # ── Outbound campaign arm switch ─────────────────────────────────────────
    # The deployment-level half of a two-key launch. False (the default) means
    # nothing prospect-facing ever leaves the system — no dial, no follow-up
    # SMS or callback, no consent request — whatever else is configured. True
    # only *permits* outbound: an operator must still authorize the campaign at
    # runtime (POST /api/outbound/resume), and can halt it at any moment
    # (POST /api/outbound/halt). See domain.outbound.
    outbound_enabled: bool = False

    # Truncates carrier_rate_tables on every boot so quotes fall back to the
    # formula adapter. Destructive: production refuses to boot with it set.
    reset_rate_table_on_boot: bool = False

    # ── Lead-use and consent authorization (domain.imports.classify) ─────────
    # Comma-separated names of YOUR agency as a consent disclosure must name it
    # (e.g. "Eastwood Financial,Eastwood Insurance Agency"). TCPA consent is
    # seller-specific: consent given to another company does not authorize you.
    # Empty means no consent can authorize AI-voice calls or marketing SMS.
    consent_authorized_sellers: str = ""
    # Consent older than this many days is quarantined (0 = no age limit).
    consent_max_age_days: int = 0

    # Scanned-PDF rows below this Tesseract confidence are imported quarantined
    # (ocr_low_confidence) until a human checks them.
    ocr_min_confidence: float = 85.0

    # Third-party enrichment (OpenFEC donor / NPPES provider lookups) sends each
    # lead's name to an external API. Off by default; never used by bulk import.
    enrichment_enabled: bool = False

    # ── Outbound spend and provider limits (domain.spend) ───────────────────
    # Global ceiling on estimated outbound spend per calendar day (agent's
    # timezone). The dialer stops for the day when the next call could exceed it.
    outbound_daily_spend_limit_usd: float = 25.0
    # Provider rate limit for call initiations (Twilio's default outbound
    # rate is about 1 call/second per account).
    dial_calls_per_minute: int = 30
    # Unit prices used to estimate spend (USD). Confirm against your invoices.
    cost_voice_per_min_usd: float = 0.014
    cost_ai_per_min_usd: float = 0.15
    cost_transcribe_per_min_usd: float = 0.006
    cost_sms_per_segment_usd: float = 0.013
    # Worst-case cost reserved for every call in flight when checking budgets.
    # The effective reserve is never below what a MAX_CALL_MINUTES call costs
    # (call_reserve_usd), which is what makes the budget ceilings hard limits.
    cost_reserve_per_call_usd: float = 1.75
    # Hard cap on a single call's length, enforced by the provider (Twilio
    # TimeLimit) — it bounds what one call can cost.
    max_call_minutes: int = 15
    # Hold dialing while the agent's own calendar has no open slot in the
    # horizon: calls that cannot end in an appointment only spend money.
    pause_dialing_when_calendar_full: bool = True

    # "twilio" (real) or "simulated" — a fake provider for load testing that
    # never touches a network beyond this stack. Production refuses "simulated".
    telephony_provider: str = "twilio"
    # Where the simulated provider delivers its signed status callbacks.
    simulated_callback_base_url: str = "http://nginx"
    # Simulated calls finish after this many seconds (wall clock).
    simulated_call_seconds: float = 2.0
    # Simulated SMS prospects (scripted personas) answer Imogen's texts through
    # the signed /twilio/sms webhook. Only with TELEPHONY_PROVIDER=simulated.
    simulated_sms_prospects: bool = False
    simulated_sms_reply_seconds: float = 2.0

    # ── Quoting policy ────────────────────────────────────────────────────────
    # Prospect-facing premiums come only from VERIFIED carrier rate sheets
    # (domain.quoting.prospect). The formula estimator is internal-only.
    prospect_quotes_require_verified: bool = True
    # Synthetic test rate sheets (acceptance testing). Refused in production.
    allow_test_rate_sheets: bool = False

    # ── SMS-first engine (domain.sms) ─────────────────────────────────────────
    # How Imogen names the agency in texts (must match how consent names it).
    agency_display_name: str = "Eastwood Insurance"
    # How texts refer to the licensed agent who takes the appointment.
    agent_display_name: str = "our licensed agent"
    # none = rule-based understanding only (no AI cost); openai = an economical
    # chat model reads free-text replies the rules cannot (OPENAI_API_KEY).
    sms_llm_provider: str = "none"
    sms_llm_model: str = "gpt-5-mini"
    sms_llm_cost_in_per_m_usd: float = 0.25
    sms_llm_cost_out_per_m_usd: float = 2.00
    # Hours after the first text to nudge a lead who never replied.
    sms_followup_hours: str = "48,120"
    # Hours after a prospect goes quiet mid-conversation to check back in.
    sms_stall_hours: str = "24,72"
    # Appointment reminders, hours before the start time.
    sms_reminder_hours: str = "24,2"
    # Answer an inbound text outside 8am-9pm local, or wait for the window.
    sms_reply_outside_hours: bool = False
    # Global text sending rate (all campaigns).
    sms_messages_per_minute: int = 60
    # Cost guard: after this many texts in one conversation, Imogen stops and
    # hands the prospect to the agent (a normal booking takes ~10-15).
    sms_max_outbound_per_conversation: int = 30
    # Times offered to one prospect are held back from other prospects for
    # this long, so a campaign burst does not offer everyone the same slots.
    sms_offer_hold_minutes: int = 15
    # Twilio inbound price per segment plus a typical inbound carrier fee.
    cost_sms_inbound_per_segment_usd: float = 0.0100

    # ── Licensed agent's own calendar (domain.scheduling.local) ─────────────
    # Weekly availability in the agent's timezone, e.g.
    #   "mon-fri 17:00-20:00; sat 09:00-12:00"
    # Empty disables the local calendar (Calendly or a free-form request is used).
    agent_availability: str = ""
    appointment_slot_minutes: int = 30
    appointment_buffer_minutes: int = 0
    appointment_max_per_day: int = 4
    appointment_min_notice_hours: int = 2
    appointment_horizon_days: int = 14
    # Comma-separated YYYY-MM-DD dates with no appointments (vacation, etc.).
    agent_blackout_dates: str = ""

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

    def call_reserve_usd(self) -> float:
        """Budget reserved per call: the cost of a call that runs the full
        MAX_CALL_MINUTES (voice billed per started minute), or the configured
        COST_RESERVE_PER_CALL_USD if higher."""
        per_min = self.cost_voice_per_min_usd + self.cost_ai_per_min_usd + self.cost_transcribe_per_min_usd
        return round(max(self.cost_reserve_per_call_usd, self.max_call_minutes * per_min), 4)

    def consent_sellers_list(self) -> list[str]:
        return [s.strip() for s in self.consent_authorized_sellers.split(",") if s.strip()]

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

    @field_validator("max_call_minutes")
    @classmethod
    def call_minutes_in_range(cls, v: int) -> int:
        if not 1 <= v <= 240:
            raise ValueError("MAX_CALL_MINUTES must be 1-240")
        return v

    @field_validator("active_states")
    @classmethod
    def active_states_are_known(cls, v: str) -> str:
        """Every campaign state must be one the TCPA calling-window table knows.

        An unknown code (a typo such as "MIC", or a territory) would otherwise
        silently cost every lead its state-fit points, and a state with no
        timezone entry cannot have its calling window enforced correctly.
        """
        from domain.tcpa import supported_states

        codes = [s.strip().upper() for s in v.split(",") if s.strip()]
        unknown = sorted(set(codes) - supported_states())
        if unknown:
            raise ValueError(
                f"ACTIVE_STATES contains unsupported state codes: {', '.join(unknown)}. "
                "Use two-letter USPS codes for the 50 states and DC."
            )
        return v

    @model_validator(mode="after")
    def warm_not_above_hot(self) -> "Settings":
        if self.score_warm_threshold > self.score_hot_threshold:
            raise ValueError(
                "SCORE_WARM_THRESHOLD must not exceed SCORE_HOT_THRESHOLD "
                f"({self.score_warm_threshold} > {self.score_hot_threshold})"
            )
        return self


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
    start with it set. DNC_TEST_ALLOWLIST scrubs listed numbers as exempt without
    a provider — a testing affordance that must never be present in production.
    """
    s = settings or get_settings()
    if s.app_env.strip().lower() == "production" and s.bypass_tcpa:
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with BYPASS_TCPA=true. "
            "This disables the TCPA calling-window check for live outbound "
            "dialing. Unset BYPASS_TCPA, or set APP_ENV to a non-production value."
        )
    if s.app_env.strip().lower() == "production" and s.dnc_test_allowlist.strip():
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with DNC_TEST_ALLOWLIST set. "
            "The allowlist scrubs listed numbers as exempt without a provider — a "
            "testing-only affordance. Clear DNC_TEST_ALLOWLIST, or set APP_ENV to a "
            "non-production value."
        )
    if s.app_env.strip().lower() == "production" and not s.twilio_signature_validation_enabled:
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with "
            "TWILIO_SIGNATURE_VALIDATION_ENABLED=false. Unauthenticated webhook "
            "callbacks would let anyone spoof call status and TwiML fetches. "
            "Re-enable validation, or set APP_ENV to a non-production value."
        )
    if s.app_env.strip().lower() == "production" and not s.eastwood_api_key.strip():
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production without EASTWOOD_API_KEY. "
            "Operational /api routes fail closed without a configured key, which "
            "would break quoting and booking mid-campaign. Set EASTWOOD_API_KEY "
            "in the environment (and give the same value to ai-agent)."
        )
    if not is_production(s):
        return

    if s.reset_rate_table_on_boot:
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with RESET_RATE_TABLE_ON_BOOT=true. "
            "It truncates carrier_rate_tables on every boot, silently discarding any "
            "loaded carrier rates. Set RESET_RATE_TABLE_ON_BOOT=false."
        )

    # National DNC: production clears numbers only against the official dataset.
    if s.dnc_scrub_provider.strip().lower() != "ftc_registry":
        raise UnsafeProductionConfigError(
            f"Refusing to start: APP_ENV=production with DNC_SCRUB_PROVIDER="
            f"{s.dnc_scrub_provider!r}. Production requires the official FTC "
            "registry provider (DNC_SCRUB_PROVIDER=ftc_registry)."
        )
    if not s.dnc_san.strip() or not s.dnc_area_codes.strip():
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with the FTC registry provider "
            "but without DNC_SAN and DNC_AREA_CODES. Without a subscription and "
            "its area-code scope no number can ever be cleared."
        )

    # Webhooks: Twilio signs the exact public URL, so it must be stable HTTPS.
    problem = public_base_url_problem(s.public_base_url)
    if problem:
        raise UnsafeProductionConfigError(
            f"Refusing to start: APP_ENV=production with an unusable PUBLIC_BASE_URL "
            f"({problem}). Twilio TwiML, status and media callbacks are built from it "
            "and signature validation reconstructs it, so it must be a stable HTTPS URL."
        )

    # Scoring: the documented launch thresholds are a floor in production.
    if (
        s.score_hot_threshold < PRODUCTION_MIN_HOT_THRESHOLD
        or s.score_warm_threshold < PRODUCTION_MIN_WARM_THRESHOLD
    ):
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with scoring thresholds below "
            f"the documented launch values (hot>={PRODUCTION_MIN_HOT_THRESHOLD}, "
            f"warm>={PRODUCTION_MIN_WARM_THRESHOLD}); got hot={s.score_hot_threshold}, "
            f"warm={s.score_warm_threshold}. Test thresholds route nearly every lead "
            "to auto-dial."
        )

    if not s.active_states_list():
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with ACTIVE_STATES empty. "
            "Name every campaign state explicitly."
        )

    if s.max_concurrent_calls < 1:
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with MAX_CONCURRENT_CALLS < 1."
        )

    if s.telephony_provider.strip().lower() != "twilio":
        raise UnsafeProductionConfigError(
            f"Refusing to start: APP_ENV=production with TELEPHONY_PROVIDER="
            f"{s.telephony_provider!r}. The simulated provider is for load testing only."
        )

    if s.outbound_daily_spend_limit_usd <= 0:
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production without a positive "
            "OUTBOUND_DAILY_SPEND_LIMIT_USD. Outbound spend must be capped."
        )

    if s.allow_test_rate_sheets:
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production with ALLOW_TEST_RATE_SHEETS=true. "
            "Synthetic rate sheets must never price a real prospect's quote."
        )

    if not s.consent_sellers_list():
        raise UnsafeProductionConfigError(
            "Refusing to start: APP_ENV=production without CONSENT_AUTHORIZED_SELLERS. "
            "Name your agency exactly as consent disclosures name it; consent given "
            "to any other seller does not authorize AI-voice calls or marketing SMS."
        )


# The scoring thresholds documented for launch (docs/go-live-checklist.md §3).
PRODUCTION_MIN_HOT_THRESHOLD = 75
PRODUCTION_MIN_WARM_THRESHOLD = 50

# Hosts that are temporary by design: they rotate, and every Twilio webhook
# 404s (or fails signature validation) the moment they do.
_TEMPORARY_TUNNEL_SUFFIXES = (
    ".ngrok.io",
    ".ngrok-free.app",
    ".ngrok.app",
    ".ngrok-free.dev",
    ".ngrok.dev",
    ".trycloudflare.com",
    ".loca.lt",
    ".localtunnel.me",
    ".serveo.net",
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}


def is_production(settings: Settings) -> bool:
    return settings.app_env.strip().lower() == "production"


def public_base_url_problem(url: str) -> str | None:
    """Why this PUBLIC_BASE_URL cannot carry production webhooks, or None if it can."""
    url = (url or "").strip()
    if not url:
        return "it is empty"
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "it is not https"
    host = (parsed.hostname or "").lower()
    if not host:
        return "it has no host"
    if host in _LOCAL_HOSTS:
        return f"{host} is a local address"
    if any(host == s.lstrip(".") or host.endswith(s) for s in _TEMPORARY_TUNNEL_SUFFIXES):
        return f"{host} is a temporary tunnel host"
    return None
