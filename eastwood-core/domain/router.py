from __future__ import annotations

from dataclasses import dataclass

from domain.scorer import ScoreResult


@dataclass(frozen=True)
class RouteDecision:
    action: str
    reason: str
    next_status: str


def route(
    score_result: ScoreResult,
    express_written: bool,
    on_suppression: bool,
    on_dnc: bool,
    manual_review_required: bool = False,
) -> RouteDecision:
    if on_suppression or on_dnc:
        return RouteDecision(
            action="suppress",
            reason="dnc_or_suppression",
            next_status="suppressed",
        )

    if manual_review_required:
        return RouteDecision(
            action="manual_review",
            reason="flagged_for_review",
            next_status="pending_review",
        )

    if score_result.band == "hot":
        if express_written:
            return RouteDecision(
                action="auto_dial",
                reason="hot_lead_with_consent",
                next_status="dialing",
            )
        return RouteDecision(
            action="request_consent_sms",
            reason="hot_lead_no_consent",
            next_status="awaiting_consent",
        )

    if score_result.band == "warm":
        if express_written:
            return RouteDecision(
                action="auto_dial",
                reason="warm_lead_with_consent",
                next_status="dialing",
            )
        return RouteDecision(
            action="request_consent_sms",
            reason="warm_lead_no_consent",
            next_status="awaiting_consent",
        )

    return RouteDecision(
        action="nurture",
        reason="cold_lead",
        next_status="nurture",
    )
