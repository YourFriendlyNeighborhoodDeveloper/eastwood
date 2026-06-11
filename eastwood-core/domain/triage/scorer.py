"""
Layer 2 — Severity scoring.

Model: worst-condition-dominates + comorbidity bump + modifiers.
Underwriting doesn't average — a single dialysis line overrides everything else.
"""
from __future__ import annotations

from datetime import date

from .models import TriageInput, TobaccoStatus


def _age_from_dob(dob: date) -> int:
    today = date.today()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def lookback_factor(
    last_event_on: date | None,
    window_years: float,
    today: date | None = None,
    outside_factor: float = 0.35,
    unknown_factor: float = 1.0,
) -> float:
    """
    Decay a condition's severity based on how long ago it last occurred.
    Unknown recency → assume recent (conservative for ranking, never for recommendation).
    """
    if last_event_on is None:
        return unknown_factor
    t = today or date.today()
    years = (t - last_event_on).days / 365.25
    if years >= window_years:
        return outside_factor
    return 1.0 - (1.0 - outside_factor) * (years / window_years)


def compute_severity(inp: TriageInput, ruleset: dict) -> tuple[float, list[str]]:
    """
    Returns (severity_score ∈ [0,1], drivers).
    Lower severity = cleaner lead = more likely LEVEL death benefit.
    GI-floor conditions push severity to T2_workable_ceiling so the engine
    can route them to GI carriers or DEAD if none available.
    """
    today = date.today()
    condition_map = {c["code"]: c for c in ruleset.get("conditions", [])}
    med_proxy_map  = {m["code"]: m for m in ruleset.get("med_proxies", [])}
    outside_factor = float(ruleset.get("lookback_outside_window_factor", 0.35))
    unknown_factor = float(ruleset.get("lookback_unknown_date_factor", 1.0))
    drivers: list[str] = []

    severities: list[float] = []

    # ── Stated conditions ─────────────────────────────────────────────────────
    for cond in inp.conditions:
        spec = condition_map.get(cond.code)
        if not spec:
            continue
        base = float(spec["base_severity"])
        window = spec.get("lookback_years")
        if window:
            factor = lookback_factor(
                cond.last_event_on, float(window), today, outside_factor, unknown_factor
            )
            eff = base * factor
            age_note = f" (event {cond.last_event_on or 'unknown'})"
            drivers.append(f"{cond.code}: base={base:.2f} × decay={factor:.2f} → {eff:.2f}{age_note}")
        else:
            eff = base
            if eff > 0.05:
                drivers.append(f"{cond.code}: severity {eff:.2f}")
        severities.append(eff)

    # ── Medication proxies ────────────────────────────────────────────────────
    for med in inp.medications:
        proxy = med_proxy_map.get(med.code)
        if not proxy or proxy.get("escalate_to_l1"):
            continue  # escalate_to_l1 meds handled in gates.py
        sev = proxy.get("severity")
        if sev is not None:
            severities.append(float(sev))
            drivers.append(f"med:{med.code} → {proxy['implies']}: severity {sev:.2f}")

    # ── Dominant condition ────────────────────────────────────────────────────
    dominant = max(severities) if severities else 0.0

    # ── Comorbidity bump ──────────────────────────────────────────────────────
    bump_rate = float(ruleset.get("comorbidity_bump_per_extra_condition", 0.10))
    bump_cap  = float(ruleset.get("comorbidity_bump_cap", 0.20))
    n_sig = sum(1 for s in severities if s > 0.10)
    comorbidity = min(bump_rate * max(0, n_sig - 1), bump_cap) if n_sig > 1 else 0.0
    if comorbidity > 0:
        drivers.append(f"comorbidity: {n_sig} significant conditions → +{comorbidity:.2f}")

    # ── Modifiers ─────────────────────────────────────────────────────────────
    mods = ruleset.get("modifiers", {})
    modifier_total = 0.0

    match inp.tobacco:
        case TobaccoStatus.CURRENT:
            delta = float(mods.get("tobacco_current", 0.10))
            modifier_total += delta
            drivers.append(f"tobacco: current +{delta:.2f}")
        case TobaccoStatus.FORMER:
            delta = float(mods.get("tobacco_former", 0.03))
            modifier_total += delta
        case TobaccoStatus.UNKNOWN:
            delta = float(mods.get("tobacco_unknown", 0.05))
            modifier_total += delta

    if inp.height_in and inp.weight_lb and inp.height_in > 0:
        bmi = (inp.weight_lb * 703) / (inp.height_in ** 2)
        if bmi >= 40:
            delta = float(mods.get("bmi_obese_3", 0.15))
            modifier_total += delta
            drivers.append(f"BMI {bmi:.0f} (class III obese) +{delta:.2f}")
        elif bmi >= 35:
            delta = float(mods.get("bmi_obese_2", 0.10))
            modifier_total += delta
            drivers.append(f"BMI {bmi:.0f} (class II obese) +{delta:.2f}")
        elif bmi >= 30:
            delta = float(mods.get("bmi_obese_1", 0.05))
            modifier_total += delta

    age = inp.age or (_age_from_dob(inp.dob) if inp.dob else None)
    if age:
        if age >= 81:
            delta = float(mods.get("age_81_plus", 0.10))
            modifier_total += delta
            drivers.append(f"age {age} (81+ band) +{delta:.2f}")
        elif 75 <= age <= 80:
            delta = float(mods.get("age_75_80", 0.05))
            modifier_total += delta

    severity = max(0.0, min(dominant + comorbidity + modifier_total, 1.0))

    # ── GI-floor push ─────────────────────────────────────────────────────────
    # These conditions are workable only via GI carriers; push to T2 ceiling so
    # the engine can route them correctly (or kill them if no carrier available).
    gi_floor: list[str] = ruleset.get("gi_floor_conditions", [])
    condition_codes = {c.code for c in inp.conditions}
    gi_hit = condition_codes & set(gi_floor)
    if gi_hit:
        thresholds = ruleset.get("thresholds", {})
        T2 = float(thresholds.get("T2_workable_ceiling", thresholds.get("T2", 0.85)))
        if severity < T2:
            drivers.append(f"GI-floor condition {next(iter(gi_hit))} → severity pushed to T2 ceiling {T2:.2f}")
            severity = T2

    return severity, drivers
