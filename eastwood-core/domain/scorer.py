from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.settings import get_settings

_FEC_DONOR_SCORE = 15
_NPI_PROVIDER_SCORE = 10
_HOME_VALUE_TIERS = [
    (750_000, "ultra_high", 15),
    (400_000, "high", 10),
    (200_000, "medium", 5),
    (0, "low", 0),
]
_CONSENT_FRESH_SCORE = 20
_CONSENT_STALE_SCORE = 5
_AGE_FIT_BANDS: dict[str, tuple[int, int, int]] = {
    "final_expense": (50, 80, 20),
    "medicare_advantage": (64, 85, 20),
    "medicare_supplement": (64, 85, 20),
    "aca": (18, 64, 15),
    "annuity": (50, 75, 15),
    "life": (25, 60, 15),
}
_STATE_FIT_SCORE = 10
_TOUCH_PENALTY_PER_RECENT = -5
_TOUCH_RECENT_WINDOW_DAYS = 30


@dataclass
class ScoreResult:
    score: int
    band: str
    components: dict[str, int] = field(default_factory=dict)
    wealth_tier: str = "low"


def _wealth_tier_score(enrichment: dict[str, Any]) -> tuple[str, int]:
    pts = 0
    tier = "low"

    if enrichment.get("fec_donor"):
        pts += _FEC_DONOR_SCORE

    if enrichment.get("npi_provider"):
        pts += _NPI_PROVIDER_SCORE

    home_value = enrichment.get("home_value") or 0
    for threshold, t, t_pts in _HOME_VALUE_TIERS:
        if home_value >= threshold:
            tier = t
            pts += t_pts
            break

    return tier, pts


def _consent_score(express_written: bool, consent_age_days: int | None) -> int:
    if not express_written:
        return 0
    if consent_age_days is None or consent_age_days <= 30:
        return _CONSENT_FRESH_SCORE
    return _CONSENT_STALE_SCORE


def _age_fit_score(product_line: str | None, age: int | None) -> int:
    if not product_line or age is None:
        return 0
    band = _AGE_FIT_BANDS.get(product_line)
    if not band:
        return 0
    min_age, max_age, pts = band
    return pts if min_age <= age <= max_age else 0


def _touch_penalty(recent_touches: int) -> int:
    return max(_TOUCH_PENALTY_PER_RECENT * recent_touches, -20)


def score_lead(
    express_written: bool,
    consent_age_days: int | None,
    product_line: str | None,
    age: int | None,
    state: str | None,
    recent_touches: int,
    enrichment: dict[str, Any] | None,
    active_states: list[str] | None = None,
) -> ScoreResult:
    settings = get_settings()
    enrichment = enrichment or {}
    active_states = active_states or []

    wealth_tier, wealth_pts = _wealth_tier_score(enrichment)
    consent_pts = _consent_score(express_written, consent_age_days)
    age_pts = _age_fit_score(product_line, age)
    state_pts = _STATE_FIT_SCORE if (state and state in active_states) else 0
    touch_pts = _touch_penalty(recent_touches)

    raw = wealth_pts + consent_pts + age_pts + state_pts + touch_pts
    score = max(0, min(100, raw))

    if score >= settings.score_hot_threshold:
        band = "hot"
    elif score >= settings.score_warm_threshold:
        band = "warm"
    else:
        band = "cold"

    return ScoreResult(
        score=score,
        band=band,
        wealth_tier=wealth_tier,
        components={
            "wealth": wealth_pts,
            "consent": consent_pts,
            "age_fit": age_pts,
            "state_fit": state_pts,
            "touch_penalty": touch_pts,
        },
    )
