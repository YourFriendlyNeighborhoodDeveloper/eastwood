"""
Data acquisition plan generator.

Given current lead + health_data, returns the ordered list of fields the voice
agent should collect — always Tier 1 screening only at session start.

Tier 2 (condition-specific follow-ups) are added dynamically via
get_followup_tasks() when the prospect confirms a condition during screening.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AcquisitionTask:
    field: str
    priority: int
    question: str       # verbatim ask for the agent
    capture_key: str    # key in health_data (or "age" → leads.age directly)
    value_hint: str = ""


_CARRIER_QUESTION_TEXT: dict[str, tuple[str, str]] = {
    "diabetes_type":         ("diabetes_details", "Is it Type 1 or Type 2, and are you on insulin?"),
    "a1c":                   ("a1c", "Do you know your most recent A1C number?"),
    "complications":         ("diabetes_complications", "Have you had any diabetes-related complications — neuropathy, eye or kidney issues?"),
    "diagnosis_date":        ("diagnosis_date", "When were you first diagnosed?"),
    "heart_attack_date":     ("heart_attack_date", "When did that happen, and have you had any procedures since — stents or bypass?"),
    "stents":                ("stents", "Did you have any stents placed?"),
    "bypass":                ("bypass", "Did you have bypass surgery?"),
    "stroke_date":           ("stroke_date", "When did the stroke occur, and do you have any lasting effects from it?"),
    "residual_deficits":     ("residual_deficits", "Any lasting weakness, speech issues, or memory changes since the stroke?"),
    "cancer_type":           ("cancer_type", "What type of cancer was it, and are you currently in treatment or remission?"),
    "treatment_end_date":    ("treatment_end_date", "When did treatment end?"),
    "remission_confirmed":   ("remission_confirmed", "Has your doctor confirmed you're in remission?"),
    "treatment_status":      ("treatment_status", "Are you currently in active treatment?"),
    "oxygen_use":            ("oxygen_use", "Do you use oxygen — even just at night?"),
    "lpm":                   ("oxygen_lpm", "How many liters per minute?"),
    "fev1":                  ("fev1", "Do you know your most recent FEV1 number from a breathing test?"),
    "hospitalizations_past_year": ("hospitalizations_past_year", "How many times have you been hospitalised in the past 12 months for that?"),
    "ejection_fraction":     ("ejection_fraction", "Do you know your ejection fraction number from your last echo?"),
    "dialysis_frequency":    ("dialysis_frequency", "How often do you go to dialysis each week?"),
    "transplant_listed":     ("transplant_listed", "Are you on a transplant waiting list?"),
    "height_weight":         ("height_weight", "What's your height and current weight?"),
    "hospitalization_recent": ("hospitalization_recent", "Have you been hospitalised in the past 12 months for any reason?"),
    "part_a_part_b":         ("medicare_parts", "Are you currently enrolled in both Medicare Part A and Part B?"),
    "current_meds":          ("current_meds", "What medications are you currently taking?"),
    "preferred_doctors":     ("preferred_doctors", "Do you have any doctors or specialists you'd need to stay in-network with?"),
    "drug_list":             ("drug_list", "Would you like me to check if your specific medications are covered?"),
}


def build_acquisition_plan(
    lead: dict,
    health_data: dict,
) -> list[AcquisitionTask]:
    """
    Returns the Tier 1 screening agenda only — sorted by priority.

    Tier 2 condition-specific follow-ups are never pre-loaded; they are added
    dynamically via get_followup_tasks() after the prospect confirms a condition.
    """
    hd = health_data or {}
    tasks: list[AcquisitionTask] = []

    # 1. Age — every rate calculation depends on it
    if not lead.get("age") and not hd.get("dob"):
        tasks.append(AcquisitionTask(
            field="age",
            priority=1,
            question=(
                "To make sure I find you the best rate — could I get your date of birth, "
                "or your age is fine too?"
            ),
            capture_key="age",
            value_hint="integer years, or date MM/DD/YYYY",
        ))

    # ── Needs discovery — what they want and who it protects. Gathered naturally,
    #    never as a checklist. A quote cannot unlock until purpose + a beneficiary
    #    + (an amount or a budget) are captured (enforced agent-side).
    if not hd.get("beneficiary_relationship") and not hd.get("beneficiary_name"):
        tasks.append(AcquisitionTask(
            field="beneficiary",
            priority=2,
            question="Who are you mainly trying to make sure is protected?",
            capture_key="beneficiary_relationship",
            value_hint="relationship (spouse, children, ...) or a name",
        ))

    if not hd.get("coverage_purpose"):
        tasks.append(AcquisitionTask(
            field="coverage_purpose",
            priority=3,
            question=(
                "Would this mostly be for burial or cremation costs, or are you "
                "trying to leave something behind for family as well?"
            ),
            capture_key="coverage_purpose",
            value_hint="burial | cremation | income_replacement | debt_mortgage | family_protection | other",
        ))

    if not hd.get("desired_coverage_amount") and not hd.get("affordable_monthly_budget"):
        tasks.append(AcquisitionTask(
            field="coverage_target",
            priority=4,
            question=(
                "Did you already have a coverage amount in mind, or were you trying "
                "to stay around a certain monthly payment?"
            ),
            capture_key="desired_coverage_amount",
            value_hint=(
                "dollar amount → capture as desired_coverage_amount; a monthly figure "
                "→ capture as affordable_monthly_budget"
            ),
        ))

    if not hd.get("existing_coverage"):
        tasks.append(AcquisitionTask(
            field="existing_coverage",
            priority=5,
            question="Do you currently have any coverage in place now?",
            capture_key="existing_coverage",
            value_hint="yes | no | unknown; if yes, also capture existing_coverage_amount",
        ))

    if not hd.get("reason_for_coverage"):
        tasks.append(AcquisitionTask(
            field="motivation",
            priority=6,
            question="What's got you looking into this now?",
            capture_key="reason_for_coverage",
            value_hint="their reason, in their own words",
        ))

    # 2. Tobacco — affects every rate band
    tobacco = (hd.get("tobacco") or "unknown").lower()
    if tobacco == "unknown":
        tasks.append(AcquisitionTask(
            field="tobacco",
            priority=7,
            question="Have you used any tobacco or nicotine products in the past 12 months?",
            capture_key="tobacco",
            value_hint="none | current | former",
        ))

    # 3. Combined health screening — single yes/no gate before any condition detail
    if not hd.get("conditions"):
        tasks.append(AcquisitionTask(
            field="health_screening",
            priority=8,
            question=(
                "Have you been diagnosed with or treated for any of the following in the last 5 years: "
                "cancer, heart disease or heart attack, diabetes, stroke, COPD, or kidney disease? "
                "Just give me the list — none is a perfectly fine answer."
            ),
            capture_key="conditions",
            value_hint="comma-separated list of conditions confirmed, or 'none'",
        ))

    # 4. Prescription medications — yes/no, then which ones if yes
    if not hd.get("current_meds"):
        tasks.append(AcquisitionTask(
            field="medications",
            priority=9,
            question=(
                "Are you currently taking any prescription medications? "
                "If so, which ones?"
            ),
            capture_key="current_meds",
            value_hint="medication names, or 'none'",
        ))

    # 5. Height and weight — used for BMI underwriting
    if not hd.get("height_weight"):
        tasks.append(AcquisitionTask(
            field="height_weight",
            priority=10,
            question="And what's your height and current weight?",
            capture_key="height_weight",
            value_hint="e.g. 5'8\" 180 lbs",
        ))

    return sorted(tasks, key=lambda t: t.priority)


def get_followup_tasks(condition: str, health_data: dict) -> list[AcquisitionTask]:
    """
    Returns Tier 2 follow-up tasks for the given confirmed condition.

    Only questions not already captured in health_data are returned.
    Call this after the prospect explicitly confirms a condition in screening.
    """
    from domain.placement.engine import CONDITION_FOLLOWUPS

    hd = health_data or {}
    q_keys = CONDITION_FOLLOWUPS.get(condition, [])
    tasks: list[AcquisitionTask] = []
    for i, q_key in enumerate(q_keys):
        capture_key, question_text = _CARRIER_QUESTION_TEXT.get(
            q_key, (q_key, f"Can you tell me more about your {q_key.replace('_', ' ')}?")
        )
        if not hd.get(capture_key):
            tasks.append(AcquisitionTask(
                field=f"followup_{q_key}",
                priority=10 + i,
                question=question_text,
                capture_key=capture_key,
            ))
    return tasks


def plan_to_context(plan: list[AcquisitionTask]) -> list[dict]:
    """Serialize as camelCase for transport to the TypeScript ai-agent."""
    return [
        {
            "field": t.field,
            "priority": t.priority,
            "question": t.question,
            "captureKey": t.capture_key,
            "valueHint": t.value_hint,
        }
        for t in plan
    ]
