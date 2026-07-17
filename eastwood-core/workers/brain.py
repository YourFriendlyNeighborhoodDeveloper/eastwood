from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from clients.ai_agent import AiAgentClient
from clients.twilio_sms import SmsClient
from config.settings import get_settings
from db.session import session_scope
from domain.compliance import BLOCKED, UNKNOWN, get_scrub_provider, is_dialable, scrub_number
from domain.normalization import normalize_lead_payload
from domain.router import route
from domain.scorer import score_lead
from domain.tcpa import evaluate_tcpa
from metrics.metrics import agent_events_received, leads_ingested, leads_routed, worker_processing_seconds
from repos.repos import CallRepo, CarrierProfileRepo, ConsentRepo, DialQueueRepo, FollowupRepo, LeadFieldEventRepo, LeadRepo, ObjectionRepo, QuoteRepo, RulesetRepo, SuppressionRepo, TriageRepo

# Retry offsets and dial eligibility live in domain.dialing so the Twilio status
# callback and the dialer share one source of truth.
from domain.dialing import RETRY_OFFSETS_HOURS as _RETRY_OFFSETS_HOURS, check_dial_eligibility
from sources.proactive import enrich_lead

logger = logging.getLogger(__name__)


def _pick(q: dict, snake: str, camel: str, default=None):
    """Read a key the agent may have sent in either casing."""
    if q.get(snake) is not None:
        return q[snake]
    if q.get(camel) is not None:
        return q[camel]
    return default


def _normalize_quote_payload(quote: dict) -> dict:
    """The TypeScript agent emits camelCase quote keys; QuoteRepo reads snake_case.

    Without this, quote_records rows persist with NULL premium/face_value.
    The original keys are preserved so quote_data keeps the raw payload.

    quote_type / rate_source / carrier_rate / bindable are persisted alongside
    the premium so a stored quote can never later be mistaken for a rate a
    carrier agreed to. A payload that omits them is assumed to be a
    non-bindable estimate — the safe reading, not the flattering one.
    """
    q = dict(quote or {})
    return {
        **q,
        "product_line":    _pick(q, "product_line", "productLine", ""),
        "face_value":      _pick(q, "face_value", "faceValue"),
        "monthly_premium": _pick(q, "monthly_premium", "monthlyPremium"),
        "carrier":         q.get("carrier"),
        "quote_type":      _pick(q, "quote_type", "quoteType", "estimated"),
        "rate_source":     _pick(q, "rate_source", "rateSource", "unknown"),
        "carrier_rate":    bool(_pick(q, "carrier_rate", "carrierRate", False)),
        "bindable":        bool(_pick(q, "bindable", "bindable", False)),
    }


class WorkersBrain:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._ai_agent = AiAgentClient()
        self._sms = SmsClient() if self._settings.twilio_account_sid else None
        self._dnc_scrub = get_scrub_provider(self._settings)
        # One outcome SMS to Ryan per connected call. Destination comes from
        # settings — never hardcoded into handlers.
        from domain.notifications import CallOutcomeNotificationService
        self._outcome_notifier = CallOutcomeNotificationService(
            self._sms, self._settings.agent_notification_phone_e164
        )

    async def _ensure_national_dnc_status(self, session, lead: Any) -> str:
        """Resolve a lead's National DNC clearance, scrubbing if still undecided.

        A decided status is never overwritten: an operator's `exempt`, or an
        earlier `clear` / `blocked`, survives a provider that has no opinion.
        Any provider failure resolves to `unknown`, which is not dialable — the
        gate fails closed.
        """
        current = getattr(lead, "national_dnc_status", None) or UNKNOWN
        if current != UNKNOWN:
            return current

        try:
            # scrub_number checks the operator test allowlist before any provider.
            result = await scrub_number(
                lead.phone_e164, provider=self._dnc_scrub, settings=self._settings
            )
        except Exception:
            logger.exception(
                "national DNC scrub failed for lead %s — treating as %s", lead.id, UNKNOWN
            )
            return UNKNOWN

        await LeadRepo(session).set_national_dnc(
            lead.id,
            result.status,
            provider=result.provider,
            checked_at=result.checked_at,
            source=result.source,
            audit=getattr(result, "audit", None),
        )
        return result.status

    async def _block_lead_national_dnc(self, session, lead_id: uuid.UUID) -> None:
        """The number is on the registry. Stop everything already scheduled for it."""
        await LeadRepo(session).update_status(lead_id, "compliance_blocked")
        await DialQueueRepo(session).cancel_for_lead(lead_id)
        await FollowupRepo(session).cancel_pending_for_lead(lead_id)
        logger.warning(
            "lead %s is on the national DNC registry — dialing and follow-ups cancelled", lead_id
        )

    async def on_raw(self, msg: dict) -> None:
        with worker_processing_seconds.labels(stage="normalize").time():
            normalized = normalize_lead_payload(msg)

        if not normalized.get("phone_e164"):
            logger.warning("dropped lead: no valid phone")
            return

        tenant_id = msg.get("tenant_id", self._settings.default_tenant_id)


        async with session_scope() as session:
            lead_repo = LeadRepo(session)
            suppression_repo = SuppressionRepo(session)
            consent_repo = ConsentRepo(session)

            suppressed = await suppression_repo.is_suppressed(tenant_id, normalized["phone_e164"])
            if suppressed:
                logger.info("lead suppressed: %s", normalized["phone_e164"])
                return

            lead = await lead_repo.upsert_raw(tenant_id, normalized)
            leads_ingested.labels(tenant_id=tenant_id, source=msg.get("source", "unknown")).inc()

            # Create consent record when the import includes express written consent.
            # This covers CSV imports via --assume-consent-source and any vendor payload
            # that includes consent_source + express_written_consent=True.
            if normalized.get("express_written_consent") and msg.get("consent_source"):
                from domain.tcpa import standard_expiry
                await consent_repo.create(
                    lead.id,
                    tenant_id,
                    {
                        "consent_type": "tcpa",
                        "express_written": True,
                        "expires_at": standard_expiry(),
                        "source_url": msg.get("consent_source_url", ""),
                        "ip_address": msg.get("consent_ip"),
                        "evidence_pack": {
                            "source":    msg.get("consent_source"),
                            "timestamp": msg.get("consent_timestamp"),
                            "raw_row":   msg.get("consent_raw_row"),
                        },
                    },
                )
                logger.info("consent record created for lead %s source=%s", lead.id, msg.get("consent_source"))

        from kafka_io.kafka_client import publish
        await publish(
            self._settings.topic_leads_normalized,
            {"lead_id": str(lead.id), "tenant_id": tenant_id, **normalized},
            key=str(lead.id),
        )

    async def score_and_route(self, msg: dict) -> None:
        from domain.triage import triage_from_lead_dict

        with worker_processing_seconds.labels(stage="score").time():
            lead_id = uuid.UUID(msg["lead_id"])
            tenant_id = msg.get("tenant_id", self._settings.default_tenant_id)

            async with session_scope() as session:
                lead_repo = LeadRepo(session)
                consent_repo = ConsentRepo(session)
                suppression_repo = SuppressionRepo(session)
                call_repo = CallRepo(session)
                triage_repo = TriageRepo(session)
                ruleset_repo = RulesetRepo(session)

                lead = await lead_repo.get_by_id(lead_id)
                if not lead:
                    return

                consent = await consent_repo.get_latest(lead_id)
                express_written = consent.express_written if consent else msg.get("express_written_consent", False)
                consent_expires_at = consent.expires_at if consent else None

                consent_age_days: int | None = None
                if consent and consent.created_at:
                    delta = datetime.now(timezone.utc) - consent.created_at.replace(tzinfo=timezone.utc)
                    consent_age_days = delta.days

                recent_touches = await call_repo.recent_count(lead_id, within_days=30)
                suppressed = await suppression_repo.is_suppressed(tenant_id, lead.phone_e164)

                enrichment_data = await enrich_lead(lead.first_name, lead.last_name, lead.state)
                await lead_repo.merge_enrichment(lead_id, enrichment_data)

                # ── SAURON triage ─────────────────────────────────────────────
                active_ruleset = await ruleset_repo.get_active()
                ruleset_payload = active_ruleset.payload if active_ruleset else None

                lead_dict = {
                    "lead_id": str(lead_id),
                    "source": lead.source or "unknown",
                    "state": lead.state,
                    "age": lead.age,
                    "on_dnc": suppressed,
                    "express_written_consent": express_written,
                }
                triage_result = triage_from_lead_dict(
                    lead=lead_dict,
                    health_data=lead.health_data or {},
                    ruleset=ruleset_payload,
                )

                await triage_repo.create(lead_id, triage_result)
                await lead_repo.update_triage(lead_id, triage_result.bucket, triage_result.priority)

                if triage_result.bucket == "DEAD":
                    await lead_repo.update_status(lead_id, "dead")
                    logger.info(
                        "lead %s triaged DEAD: %s",
                        lead_id, triage_result.dead_reason or "no reason given",
                    )
                    leads_routed.labels(tenant_id=tenant_id, action="dead", band="dead").inc()
                    from kafka_io.kafka_client import publish
                    await publish(
                        self._settings.topic_leads_routed,
                        {
                            "lead_id": str(lead_id),
                            "tenant_id": tenant_id,
                            "action": "dead",
                            "reason": triage_result.dead_reason,
                            "triage_bucket": "DEAD",
                            "drivers": triage_result.drivers,
                        },
                        key=str(lead_id),
                    )
                    return

                # ── Legacy scorer (band/score for routing thresholds) ─────────
                score_result = score_lead(
                    express_written=express_written,
                    consent_age_days=consent_age_days,
                    product_line=lead.product_line,
                    age=lead.age,
                    state=lead.state,
                    recent_touches=recent_touches,
                    enrichment={**(lead.enrichment or {}), **enrichment_data},
                    active_states=self._settings.active_states_list(),
                )

                await lead_repo.update_score(
                    lead_id, score_result.score, score_result.band, score_result.components
                )

                # ── National DNC Registry gate ───────────────────────────────
                # Scored above regardless: an unscrubbed lead is worth storing and
                # ranking, it just may not be dialed. Only `blocked` stops here.
                national_status = await self._ensure_national_dnc_status(session, lead)
                if national_status == BLOCKED:
                    await self._block_lead_national_dnc(session, lead_id)
                    leads_routed.labels(
                        tenant_id=tenant_id, action="compliance_blocked", band=score_result.band
                    ).inc()
                    from kafka_io.kafka_client import publish
                    await publish(
                        self._settings.topic_leads_routed,
                        {
                            "lead_id": str(lead_id),
                            "tenant_id": tenant_id,
                            "action": "compliance_blocked",
                            "reason": "national_dnc_registry",
                            "national_dnc_status": national_status,
                            "score": score_result.score,
                            "band": score_result.band,
                            "triage_bucket": triage_result.bucket,
                        },
                        key=str(lead_id),
                    )
                    return

                tcpa_result = evaluate_tcpa(
                    action="outbound_dial",
                    express_written=express_written,
                    consent_expires_at=consent_expires_at,
                    on_dnc=suppressed,
                )

                decision = route(
                    score_result=score_result,
                    express_written=express_written,
                    on_suppression=suppressed,
                    on_dnc=not tcpa_result.allowed and suppressed,
                )

                await lead_repo.update_status(lead_id, decision.next_status)

        leads_routed.labels(
            tenant_id=tenant_id, action=decision.action, band=score_result.band
        ).inc()

        from kafka_io.kafka_client import publish
        await publish(
            self._settings.topic_leads_routed,
            {
                "lead_id": str(lead_id),
                "tenant_id": tenant_id,
                "action": decision.action,
                "reason": decision.reason,
                "score": score_result.score,
                "band": score_result.band,
                "triage_bucket": triage_result.bucket,
                "triage_priority": triage_result.priority,
                "triage_drivers": triage_result.drivers[:3],
            },
            key=str(lead_id),
        )

        if decision.action == "auto_dial":
            await self.enqueue_call(lead, tenant_id, priority=triage_result.priority)
        elif decision.action == "request_consent_sms":
            await self._send_consent_sms(lead)

    async def on_agent_event(self, msg: dict) -> None:
        # TypeScript bridge publishes camelCase keys; support both for safety
        event_type = msg.get("eventType") or msg.get("event_type", "unknown")
        agent_events_received.labels(event_type=event_type).inc()

        lead_id_str = msg.get("leadId") or msg.get("lead_id")
        if not lead_id_str:
            return

        lead_id = uuid.UUID(lead_id_str)
        session_id = msg.get("sessionId") or msg.get("session_id")

        # Set by the call_ended branch; the outcome SMS sends only after the
        # session_scope block commits the profile write.
        outcome_profile: dict | None = None
        outcome_value: str | None = None

        async with session_scope() as session:
            if event_type == "quote_presented":
                quote_repo = QuoteRepo(session)
                await quote_repo.create(
                    lead_id, session_id or "", _normalize_quote_payload(msg.get("quote", {}))
                )
                logger.info("quote persisted for lead %s session=%s", lead_id, session_id)

            elif event_type == "objection_captured":
                objection_repo = ObjectionRepo(session)
                await objection_repo.create(
                    lead_id,
                    session_id or "",
                    msg.get("objectionType") or msg.get("objection_type", "unknown"),
                    msg.get("text"),
                )

            elif event_type == "consent_captured":
                consent_repo = ConsentRepo(session)
                from domain.tcpa import standard_expiry
                consent_type = (
                    msg.get("consentType")
                    or msg.get("consent_type")
                    or "contact_consent"
                )
                await consent_repo.create(
                    lead_id,
                    msg.get("tenantId") or msg.get("tenant_id", self._settings.default_tenant_id),
                    {
                        "consent_type": consent_type,
                        "express_written": True,
                        "expires_at": standard_expiry(),
                        "evidence_pack": msg.get("evidence"),
                    },
                )

            elif event_type == "dnc_request":
                lead_repo = LeadRepo(session)
                suppression_repo = SuppressionRepo(session)
                dq_repo = DialQueueRepo(session)

                tenant_id = (
                    msg.get("tenantId")
                    or msg.get("tenant_id")
                    or self._settings.default_tenant_id
                )
                lead = await lead_repo.get_by_id(lead_id)
                if not lead:
                    logger.error("dnc_request for unknown lead %s — nothing suppressed", lead_id)
                    return

                # Suppress the number we dialled, never the one the model read back
                # to us. A misheard digit would suppress a stranger and leave this
                # prospect dialable.
                phone = lead.phone_e164
                claimed = msg.get("phoneE164") or msg.get("phone_e164")
                if claimed and claimed != phone:
                    logger.warning(
                        "dnc_request phone mismatch on lead %s: agent reported %s, suppressing %s",
                        lead_id, claimed, phone,
                    )

                if not await suppression_repo.is_suppressed(tenant_id, phone):
                    await suppression_repo.add(
                        tenant_id,
                        phone,
                        reason="dnc_request",
                        notes=f"requested on call, session={session_id}",
                    )
                    logger.info("suppressed %s (lead %s) — DNC requested on call", phone, lead_id)

                await lead_repo.update_status(lead_id, "suppressed")
                await dq_repo.cancel_for_lead(lead_id)

            elif event_type == "live_transfer_requested":
                await self._handle_live_transfer(lead_id, session_id, msg)

            elif event_type == "appointment_booked":
                if self._sms:
                    lead_repo = LeadRepo(session)
                    lead = await lead_repo.get_by_id(lead_id)
                    if lead:
                        msg_body = (
                            f"Hi {lead.first_name or 'there'}, your appointment with "
                            f"Eastwood Financial is confirmed! We'll call you at this number. "
                            f"Reply STOP to opt out."
                        )
                        try:
                            await asyncio.to_thread(self._sms.send, lead.phone_e164, msg_body)
                        except Exception as exc:
                            logger.exception("appointment confirmation SMS failed: %s", exc)
                # Cancel any pending redial attempts — they booked
                dq_repo = DialQueueRepo(session)
                await dq_repo.cancel_for_lead(lead_id)

            elif event_type in ("call_ended", "transferred"):
                call_repo = CallRepo(session)
                outcome = msg.get("outcome")
                duration = msg.get("durationSeconds") or msg.get("duration_seconds")
                if call_id_str := msg.get("callId") or msg.get("call_id"):
                    await call_repo.update_outcome(
                        uuid.UUID(call_id_str),
                        status=event_type,
                        outcome=outcome,
                        duration=duration,
                    )
                else:
                    # AI-agent calls carry no callId — without this record the
                    # lead has no call history and the cockpit prep sheet shows
                    # a conversation that never happened.
                    record = await call_repo.create(
                        lead_id,
                        msg.get("tenantId") or msg.get("tenant_id") or self._settings.default_tenant_id,
                        session_id=session_id,
                    )
                    await call_repo.update_outcome(
                        record.id, status=event_type, outcome=outcome, duration=duration
                    )
                    notes = (
                        msg.get("prospectProfile") or msg.get("prospect_profile") or {}
                    ).get("follow_up_notes")
                    if isinstance(notes, str) and notes.strip() and notes.strip().lower() != "unknown":
                        record.ai_summary = notes.strip()

                # The agent attaches the full ProspectProfile it assembled from
                # captured facts to every connected call's call_ended. Persist it
                # as the canonical record (once per session), then — after this
                # transaction commits — send Ryan the one outcome SMS. A call
                # that never connected publishes no call_ended and therefore
                # fabricates no profile and sends nothing.
                profile = msg.get("prospectProfile") or msg.get("prospect_profile")
                if isinstance(profile, dict) and profile:
                    from sqlalchemy import select as _sel
                    from db.models import HandoffProfile as _HP
                    existing = None
                    if session_id:
                        row = await session.execute(
                            _sel(_HP).where(_HP.session_id == session_id, _HP.appointment_id.is_(None))
                        )
                        existing = row.scalars().first()
                    if existing is None:
                        session.add(_HP(
                            lead_id=lead_id,
                            appointment_id=None,
                            tenant_id=msg.get("tenantId") or self._settings.default_tenant_id,
                            session_id=session_id,
                            profile=profile,
                        ))
                    outcome_profile = profile
                    outcome_value = outcome

                # Schedule follow-up sequence based on outcome
                if outcome:
                    lead_repo = LeadRepo(session)
                    followup_repo = FollowupRepo(session)
                    lead = await lead_repo.get_by_id(lead_id)
                    if lead:
                        from domain.followup import schedule_for_outcome
                        context = self._followup_context(lead, msg, profile if isinstance(profile, dict) else None)
                        await schedule_for_outcome(
                            lead_id=lead_id,
                            tenant_id=self._settings.default_tenant_id,
                            outcome=outcome,
                            context_data=context,
                            followup_repo=followup_repo,
                        )

                # Dial retries for unanswered calls are owned by the Twilio status
                # callback (domain.dialing.handle_terminal_status), which is the
                # authoritative terminal signal. call_ended only fires for calls
                # that reached a person, so it drives outcome follow-up, not redials.

            elif event_type == "field_captured":
                lead_repo = LeadRepo(session)
                field_event_repo = LeadFieldEventRepo(session)
                field = msg.get("field") or msg.get("fieldName")
                capture_key = msg.get("captureKey") or msg.get("capture_key")
                value = msg.get("value")
                call_id = msg.get("callId") or msg.get("call_id")

                if field == "age" and value is not None:
                    try:
                        age_val = int(value)
                        lead = await lead_repo.get_by_id(lead_id)
                        old_age = lead.age if lead else None
                        await lead_repo.update_age(lead_id, age_val)
                        await field_event_repo.create(
                            lead_id=lead_id,
                            field_name="age",
                            capture_key="age",
                            new_value=age_val,
                            old_value=old_age,
                            source=session_id,
                            call_id=call_id,
                        )
                        logger.info("captured age=%s for lead %s", value, lead_id)
                    except (ValueError, TypeError):
                        logger.warning("invalid age value '%s' for lead %s", value, lead_id)
                elif capture_key and value is not None:
                    lead = await lead_repo.get_by_id(lead_id)
                    if lead:
                        hd = dict(lead.health_data or {})
                        old_value = hd.get(capture_key)
                        # conditions come in as a comma-separated string from the agent
                        if capture_key == "conditions" and isinstance(value, str):
                            existing = hd.get("conditions") or []
                            new_conditions = [
                                {"code": c.strip().lower().replace(" ", "_"), "raw_text": c.strip()}
                                for c in value.split(",")
                                if c.strip() and c.strip().lower() != "none"
                            ]
                            hd["conditions"] = existing + new_conditions
                            new_value = hd["conditions"]
                        else:
                            hd[capture_key] = value
                            new_value = value
                        await lead_repo.update_health_data(lead_id, hd)
                        await field_event_repo.create(
                            lead_id=lead_id,
                            field_name=field or capture_key,
                            capture_key=capture_key,
                            new_value=new_value,
                            old_value=old_value,
                            source=session_id,
                            call_id=call_id,
                        )
                        logger.info("captured field=%s for lead %s", field, lead_id)

        # Outside the transaction: the profile write has committed, so the alert
        # can never describe a record that failed to persist. Fail-soft and
        # deduplicated per session inside the notifier.
        if outcome_profile is not None:
            try:
                self._outcome_notifier.send_outcome(
                    outcome_profile, session_id=session_id, outcome=outcome_value
                )
            except Exception:
                logger.exception("outcome SMS failed for session %s", session_id)

    def _followup_context(self, lead: Any, msg: dict, profile: dict | None) -> dict:
        """Follow-up template context. Quote figures come ONLY from the quote
        actually presented on the call (the persisted profile) — an SMS
        follow-up never invents a new quote."""
        quote_amount = None
        face_value = None
        carrier = None
        quote_label = None
        if profile:
            options = [o for o in (profile.get("quote_options") or []) if isinstance(o, dict)]
            selected = profile.get("selected_option")
            chosen = next(
                (o for o in options if str(o.get("label", "")).upper() == str(selected or "").upper()),
                None,
            ) or next((o for o in options if str(o.get("label", "")).upper() == "B"), None) \
              or (options[0] if options else None)
            if chosen:
                quote_amount = chosen.get("monthly_premium")
                face_value = chosen.get("face_value")
                quote_label = "carrier-backed" if chosen.get("quote_type") == "carrier" else "estimate"
            else:
                quote_amount = profile.get("quoted_monthly_premium")
                face_value = profile.get("quote_amount")
                if quote_amount is not None:
                    quote_label = "carrier-backed" if profile.get("quote_type") == "carrier" else "estimate"
            carrier = profile.get("carrier_name")
        if quote_amount is None:
            quote_amount = msg.get("quoteAmount") or msg.get("quote_amount")
            face_value = face_value or msg.get("faceValue") or msg.get("face_value")
        return {
            "first_name":      lead.first_name or "there",
            "quote_amount":    quote_amount,
            "face_value":      face_value,
            "quote_label":     quote_label or "estimate",
            "carrier":         carrier or msg.get("carrier")
                               or (lead.health_data or {}).get("placed_carrier") or "carrier TBD",
            "calendly_link":   self._settings.calendly_event_type_url,
            "callback_window": msg.get("callbackWindow", "tomorrow"),
            "last_objection":  msg.get("lastObjection"),
        }

    async def enqueue_call(self, lead: Any, tenant_id: str, priority: float = 0.0) -> None:
        """Add a lead to the dial queue for autonomous dialing. Called by score_and_route.

        Refuses any lead without National DNC clearance. The lead is re-read here
        because the scrub writes with an UPDATE — the caller's ORM copy still
        carries the pre-scrub status.
        """
        async with session_scope() as session:
            lead_repo = LeadRepo(session)
            fresh = await lead_repo.get_by_id(lead.id)
            national_status = (getattr(fresh, "national_dnc_status", None) if fresh else None) or UNKNOWN
            if not is_dialable(national_status):
                logger.warning(
                    "refusing to queue lead %s: national_dnc_status=%s", lead.id, national_status
                )
                return

            dq_repo = DialQueueRepo(session)
            entry = await dq_repo.enqueue(
                lead_id=lead.id,
                tenant_id=tenant_id,
                priority=priority,
                scheduled_at=datetime.now(timezone.utc),
                max_attempts=self._settings.dial_max_attempts,
            )
            logger.info("lead %s enqueued for dialing (entry=%s priority=%.3f)", lead.id, entry.id, priority)

    async def process_dial_queue(self) -> int:
        """Drain due dial-queue entries respecting TCPA windows and the concurrency cap.
        Returns the number of calls initiated. Called by the background worker loop."""
        from domain.tcpa import in_tcpa_window, next_tcpa_window_open

        now = datetime.now(timezone.utc)
        calls_fired = 0

        async with session_scope() as session:
            dq_repo = DialQueueRepo(session)
            lead_repo = LeadRepo(session)

            active = await dq_repo.count_active()
            slots = self._settings.max_concurrent_calls - active
            if slots <= 0:
                return 0

            entries = await dq_repo.get_due(now=now, limit=slots * 3)

            for entry in entries:
                if calls_fired >= slots:
                    break
                lead = await lead_repo.get_by_id(entry.lead_id)
                if not lead:
                    await dq_repo.mark_completed(entry.id)
                    continue

                # Last gate before the dial — re-checks every hard eligibility rule
                # (national DNC, entity suppression, terminal lead status), so a
                # scheduled retry that became ineligible in the meantime is dropped
                # before Twilio is ever called.
                eligible, reason = await check_dial_eligibility(session, lead)
                if not eligible:
                    term = "compliance_blocked" if reason.startswith("national_dnc") else "ineligible"
                    await dq_repo.mark_terminal(entry.id, term)
                    logger.warning("dial refused for lead %s: %s", lead.id, reason)
                    continue

                if not in_tcpa_window(lead.state, now):
                    next_open = next_tcpa_window_open(lead.state, now)
                    await dq_repo.reschedule(entry.id, next_open, entry.attempt_count)
                    logger.debug("lead %s outside TCPA window, rescheduled to %s", lead.id, next_open)
                    continue

                # Mark dialing with a placeholder; session_id filled in after session creation
                await dq_repo.mark_dialing(entry.id, session_id=f"pending_{entry.id}")
                calls_fired += 1
                # Fire in background so the session lock doesn't block the loop
                asyncio.create_task(
                    self._initiate_call(lead, entry.tenant_id, dial_queue_entry_id=entry.id)
                )

        return calls_fired

    async def on_amd_result(self, session_id: str, answered_by: str, call_sid: str) -> None:
        """Handle Twilio AMD callback. Called from the AMD webhook route."""
        is_machine = answered_by.startswith("machine")

        async with session_scope() as session:
            dq_repo = DialQueueRepo(session)
            entry = await dq_repo.get_by_session(session_id)
            if not entry:
                return

            if is_machine:
                await dq_repo.mark_voicemail(entry.id)
                # End the in-progress Twilio call
                if self._sms and call_sid:
                    try:
                        await asyncio.to_thread(
                            self._sms._client.calls(call_sid).update,
                            status="completed",
                        )
                    except Exception as exc:
                        logger.warning("could not hang up AMD machine call %s: %s", call_sid, exc)
                # Reschedule if attempts remain
                if entry.attempt_count < entry.max_attempts:
                    offset_hours = _RETRY_OFFSETS_HOURS[
                        min(entry.attempt_count, len(_RETRY_OFFSETS_HOURS) - 1)
                    ]
                    retry_at = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
                    await dq_repo.reschedule(entry.id, retry_at, entry.attempt_count + 1)
                    logger.info("voicemail detected — lead rescheduled in %dh", offset_hours)
            else:
                logger.info("AMD confirmed human for session %s", session_id)

    async def on_quote_requested(self, msg: dict) -> None:
        from domain.placement import place as placement_place

        correlation_id = msg.get("correlationId")
        lead_id_str = msg.get("leadId")
        session_id = msg.get("sessionId")
        if not correlation_id or not lead_id_str:
            logger.warning("quote_requested missing correlationId or leadId: %s", msg)
            return

        lead_id = uuid.UUID(lead_id_str)
        product_line = msg.get("productLine", "final_expense")
        face_value_dollars = msg.get("faceValue", 10_000)

        async with session_scope() as session:
            lead_repo = LeadRepo(session)
            carrier_repo = CarrierProfileRepo(session)
            lead = await lead_repo.get_by_id(lead_id)

            hd = (lead.health_data or {}) if lead else {}
            resolved_age = msg.get("age") or (lead.age if lead else None)
            resolved_state = msg.get("state") or (lead.state if lead else None)

            condition_codes: set[str] = {c["code"] for c in hd.get("conditions", []) if "code" in c}
            medication_codes: set[str] = {m["code"] for m in hd.get("medications", []) if "code" in m}
            db_profiles = await carrier_repo.get_active_for_product(product_line)
            placement = placement_place(
                lead_id=lead_id_str,
                age=resolved_age,
                state=resolved_state,
                product_line=product_line,
                condition_codes=condition_codes,
                medication_codes=medication_codes,
                tobacco=hd.get("tobacco", "unknown"),
                face_value=face_value_dollars,
                db_carrier_profiles=db_profiles if db_profiles else None,
            )

        top_option = placement.options[0] if placement.options else None
        from kafka_io.kafka_client import publish
        await publish(
            self._settings.topic_calls_quote_ready,
            {
                "correlationId": correlation_id,
                "leadId": lead_id_str,
                "sessionId": session_id,
                "quote": {
                    "productLine": product_line,
                    "faceValue": face_value_dollars,
                    "monthlyPremium": top_option.monthly_premium_est if top_option else 99.0,
                    "carrier": top_option.carrier if top_option else "Eastwood Preferred",
                    "rateTier": top_option.rate_tier if top_option else "unknown",
                    "termYears": None,
                },
            },
            key=lead_id_str,
        )
        logger.info(
            "quote_ready correlationId=%s carrier=%s premium=%.2f",
            correlation_id,
            top_option.carrier if top_option else "unknown",
            top_option.monthly_premium_est if top_option else 99.0,
        )

    async def _initiate_call(self, lead: Any, tenant_id: str, dial_queue_entry_id: uuid.UUID | None = None) -> None:
        from domain.acquisition import build_acquisition_plan, plan_to_context
        from domain.placement import place as placement_place
        try:
            hd = lead.health_data or {}
            condition_codes = {c["code"] for c in hd.get("conditions", []) if "code" in c}
            medication_codes = {m["code"] for m in hd.get("medications", []) if "code" in m}
            already_captured = set(hd.keys()) | ({"age"} if lead.age else set())

            async with session_scope() as _cp_session:
                _carrier_repo = CarrierProfileRepo(_cp_session)
                db_profiles = await _carrier_repo.get_active_for_product(
                    lead.product_line or "final_expense"
                )

            placement = placement_place(
                lead_id=str(lead.id),
                age=lead.age,
                state=lead.state,
                product_line=lead.product_line,
                condition_codes=condition_codes,
                medication_codes=medication_codes,
                tobacco=hd.get("tobacco", "unknown"),
                already_captured=already_captured,
                triage_bucket=lead.triage_bucket,
                db_carrier_profiles=db_profiles if db_profiles else None,
            )

            plan = build_acquisition_plan(
                lead={"age": lead.age},
                health_data=hd,
            )
            context = {
                "phone_e164":       lead.phone_e164,
                "first_name":       lead.first_name,
                "last_name":        lead.last_name,
                "product_line":     lead.product_line,
                "state":            lead.state,
                "age":              lead.age,
                "score":            lead.score,
                "tenant_id":        tenant_id,
                "triage_bucket":    lead.triage_bucket,
                "placement": {
                    "top_carrier":   placement.top_carrier,
                    "top_tier":      placement.top_tier,
                    "gi_available":  placement.gi_available,
                },
                "acquisition_plan": plan_to_context(plan),
            }
            session_resp = await self._ai_agent.start_session(str(lead.id), context)
            session_id = session_resp.get("session_id")

            # Update the dial_queue entry with the real session_id so the
            # Twilio status callback can look it up by session_id.
            if dial_queue_entry_id and session_id:
                async with session_scope() as _dq_session:
                    from repos.repos import DialQueueRepo as _DQRepo
                    await _DQRepo(_dq_session).update_session_id(dial_queue_entry_id, session_id)

            public_base = self._settings.public_base_url.rstrip("/")
            if public_base and self._sms and session_id:
                twiml_url = f"{public_base}/twiml/call/{session_id}"
                status_callback_url = f"{public_base}/twilio/status/{session_id}"
                await asyncio.to_thread(
                    self._sms.make_call,
                    lead.phone_e164,
                    twiml_url,
                    status_callback_url,
                )
                logger.info(
                    "outbound call queued lead=%s session=%s to=%s",
                    lead.id, session_id, lead.phone_e164,
                )
            else:
                logger.info(
                    "session %s created for lead %s — set PUBLIC_BASE_URL to enable outbound dialing",
                    session_id, lead.id,
                )
        except Exception as exc:
            logger.exception("failed to initiate call for lead %s: %s", lead.id, exc)

    async def _handle_live_transfer(self, lead_id: uuid.UUID, session_id: str | None, msg: dict) -> None:
        """
        Hot prospect is ready — bridge Ryan into the call right now.
        1. Notify Ryan via SMS with a one-line prospect brief.
        2. Twilio conference bridge: merge the live call + Ryan's inbound.
        3. Publish live_transfer_bridged event so the AI agent steps back.
        """
        if not self._sms:
            logger.info("live transfer requested for lead %s but SMS not configured", lead_id)
            return

        async with session_scope() as session:
            lead_repo = LeadRepo(session)
            lead = await lead_repo.get_by_id(lead_id)
            if not lead:
                return

        ryan_number = self._settings.ryan_phone_number
        if not ryan_number:
            logger.warning("live transfer requested but RYAN_PHONE_NUMBER not set")
            return

        hd = lead.health_data or {}
        carrier = msg.get("topCarrier") or hd.get("placed_carrier") or "TBD"
        tier = msg.get("topTier") or hd.get("placed_tier") or "TBD"
        quote = msg.get("quoteAmount")
        brief = (
            f"🔥 LIVE TRANSFER — {lead.first_name or ''} {lead.last_name or ''} | "
            f"Age {lead.age} | {lead.state} | {lead.product_line or 'FE'} | "
            f"Carrier: {carrier} ({tier})"
            + (f" | ~${quote}/mo" if quote else "")
            + f" | Call in now: {lead.phone_e164}"
        )

        try:
            await asyncio.to_thread(self._sms.send, ryan_number, brief)
            logger.info("live transfer SMS sent to Ryan for lead %s", lead_id)
        except Exception as exc:
            logger.exception("failed to send live transfer SMS: %s", exc)

        # Publish back so the TypeScript agent knows Ryan is joining
        from kafka_io.kafka_client import publish
        await publish(
            self._settings.topic_agent_events,
            {
                "eventType": "live_transfer_bridged",
                "leadId":    str(lead_id),
                "sessionId": session_id,
                "ryanNotified": True,
            },
            key=str(lead_id),
        )

    async def _send_consent_sms(self, lead: Any) -> None:
        if not self._sms:
            logger.info("SMS client not configured, skipping consent SMS")
            return
        try:
            body = (
                f"Hi {lead.first_name or 'there'}, you recently requested insurance information. "
                "Reply YES to receive a call about your options. "
                "Reply STOP to opt out. Msg&Data rates may apply."
            )
            await asyncio.to_thread(self._sms.send, lead.phone_e164, body)
        except Exception as exc:
            logger.exception("failed to send consent SMS to %s: %s", lead.phone_e164, exc)
