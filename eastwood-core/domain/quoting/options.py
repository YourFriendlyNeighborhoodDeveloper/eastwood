"""
Three-option (C/B/A) quote generation.

The quote engine — not the language model — selects face amounts, premiums,
option ordering, and eligibility. The model only explains the deterministic
results. Given the prospect's stated need (purpose, desired amount) and stated
affordability (monthly budget), this module picks three face amounts:

  Option C — minimum practical coverage: solves the basic stated need, never
             below the configured product minimum.
  Option B — recommended middle: the stated need with a reasonable cushion.
  Option A — maximum reasonable: the largest face within the prospect's stated
             budget (when known) and product limits.

Options are returned in presentation order C, B, A. Every face is a standard
grid value inside the configured product limits. An option whose real quoted
premium exceeds the stated budget is flagged within_budget=False — it may only
be explored with the prospect's explicit permission, never silently recommended.

Affordability *sizing* uses the formula curve (an estimate); the premium the
prospect actually hears comes from the full adapter stack per face, carrying
the same honesty labels (quote_type / rate_source / carrier_rate / bindable)
as any single quote.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace

from .adapters.formula import FormulaAdapter
from .base import CarrierQuote, QuoteRequest

# Standard face-amount grid (dollars). Options snap to these values, clamped
# to the configured product limits.
FACE_GRID: list[int] = [5_000, 10_000, 15_000, 20_000, 25_000, 30_000, 40_000, 50_000]

# Default planning targets (dollars) when the prospect stated no amount.
# Cremation starts around a $10,000 planning target; burial evaluates higher.
CREMATION_DEFAULT_TARGET = 10_000
BURIAL_DEFAULT_TARGET = 15_000
GENERIC_DEFAULT_TARGET = 10_000


@dataclass
class QuoteOption:
    """One presentable option. `quote` carries the honesty labels."""

    label: str  # "C" | "B" | "A"
    face_value_dollars: int
    quote: CarrierQuote | None
    # True/False when a budget was stated; None when affordability is unknown.
    within_budget: bool | None

    def to_payload(self) -> dict:
        q = self.quote
        return {
            "label": self.label,
            "face_value": self.face_value_dollars,
            "monthly_premium": q.monthly_premium if q else None,
            "carrier": q.carrier if q else None,
            "rate_tier": q.rate_tier if q else None,
            "within_budget": self.within_budget,
            **(q.labels() if q else {}),
        }


def _grid(min_face: int, max_face: int) -> list[int]:
    faces = [f for f in FACE_GRID if min_face <= f <= max_face]
    return faces or [min_face]


def _snap_up(target: int, faces: list[int]) -> int:
    """Smallest grid face that covers the target; the largest face if none do."""
    for f in faces:
        if f >= target:
            return f
    return faces[-1]


def _step_up(face: int, faces: list[int], steps: int = 1) -> int:
    idx = faces.index(face) if face in faces else 0
    return faces[min(idx + steps, len(faces) - 1)]


def need_target(
    coverage_purpose: str | None,
    burial_or_cremation: str | None,
    desired_amount: int | None,
) -> int:
    """The basic stated need in dollars. Prospect's number always wins."""
    if desired_amount and desired_amount > 0:
        return int(desired_amount)
    pref = (burial_or_cremation or "").strip().lower()
    purpose = (coverage_purpose or "").strip().lower()
    if pref == "cremation" or purpose == "cremation":
        return CREMATION_DEFAULT_TARGET
    if pref == "burial" or purpose == "burial":
        return BURIAL_DEFAULT_TARGET
    return GENERIC_DEFAULT_TARGET


async def _sizing_premium(request: QuoteRequest, face_dollars: int) -> float | None:
    """Cheapest formula estimate for a face — used only to size Option A."""
    sized = replace(request, face_value=face_dollars * 100)
    quotes = await FormulaAdapter().get_quotes(sized)
    return quotes[0].monthly_premium if quotes else None


async def select_face_targets(
    request: QuoteRequest,
    *,
    coverage_purpose: str | None,
    burial_or_cremation: str | None,
    desired_amount: int | None,
    monthly_budget: float | None,
    min_face: int,
    max_face: int,
) -> dict[str, int]:
    """Deterministically choose the C/B/A face amounts (dollars).

    C covers the basic need, B adds one grid step of cushion, and A is the
    largest face the stated budget can carry (or one step above B when no
    budget was stated). Always C <= B <= A, all inside product limits.
    """
    faces = _grid(min_face, max_face)
    c_face = _snap_up(need_target(coverage_purpose, burial_or_cremation, desired_amount), faces)
    b_face = _step_up(c_face, faces)

    if monthly_budget and monthly_budget > 0:
        affordable = [f for f in faces if ((p := await _sizing_premium(request, f)) is not None and p <= monthly_budget)]
        a_face = max(affordable) if affordable else faces[0]
        # A is "the maximum within affordability" — but never below the basic
        # need option; if the budget can't even carry C, keep A at C and let
        # within_budget flag it for an honest conversation.
        a_face = max(a_face, c_face)
    else:
        a_face = _step_up(b_face, faces)

    b_face = min(max(b_face, c_face), a_face) if a_face > c_face else c_face
    return {"C": c_face, "B": b_face, "A": a_face}


async def build_quote_options(
    manager,
    request: QuoteRequest,
    *,
    coverage_purpose: str | None = None,
    burial_or_cremation: str | None = None,
    desired_amount: int | None = None,
    monthly_budget: float | None = None,
    min_face: int = 5_000,
    max_face: int = 50_000,
) -> list[QuoteOption]:
    """Quote the three selected faces through the full adapter stack.

    Returns options in presentation order C, B, A. Faces that collapse to the
    same grid value are deduplicated (a tight budget can leave fewer than three
    meaningfully distinct options — never invent spread that isn't there).
    An empty list means the engine produced no quotes at all.
    """
    targets = await select_face_targets(
        request,
        coverage_purpose=coverage_purpose,
        burial_or_cremation=burial_or_cremation,
        desired_amount=desired_amount,
        monthly_budget=monthly_budget,
        min_face=min_face,
        max_face=max_face,
    )

    distinct_faces = sorted(set(targets.values()))

    async def _quote_face(face_dollars: int) -> CarrierQuote | None:
        response = await manager.get_quotes(replace(request, face_value=face_dollars * 100))
        return response.best

    quoted = dict(
        zip(distinct_faces, await asyncio.gather(*[_quote_face(f) for f in distinct_faces]))
    )

    options: list[QuoteOption] = []
    seen_faces: set[int] = set()
    for label in ("C", "B", "A"):
        face = targets[label]
        if face in seen_faces:
            continue
        seen_faces.add(face)
        quote = quoted.get(face)
        if quote is None:
            continue
        within = None
        if monthly_budget and monthly_budget > 0:
            within = quote.monthly_premium <= monthly_budget
        options.append(
            QuoteOption(label=label, face_value_dollars=face, quote=quote, within_budget=within)
        )
    return options
