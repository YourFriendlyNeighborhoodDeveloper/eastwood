"""
Data acquisition plan generator.

Given current lead + health_data, returns the ordered list of missing fields
the voice agent should collect — highest priority first.

The plan is computed stateless, injected into the agent session context,
and executed by the agent in order before quoting.
"""
from __future__ import annotations

from dataclasses import dataclass, field


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
    "hospitalizations_past_year": ("hospitalizations_past_year", "How many times have you been hospitalized in the past 12 months for that?"),
    "ejection_fraction":     ("ejection_fraction", "Do you know your ejection fraction number from your last echo?"),
    "dialysis_frequency":    ("dialysis_frequency", "How often do you go to dialysis each week?"),
    "transplant_listed":     ("transplant_listed", "Are you on a transplant waiting list?"),
    "height_weight":         ("height_weight", "What is your height and current weight?"),
    "hospitalization_recent": ("hospitalization_recent", "Have you been hospitalized in the past 12 months for any reason?"),
    "part_a_part_b":         ("medicare_parts", "Are you currently enrolled in both Medicare Part A and Part B?"),
    "current_meds":          ("current_meds", "What medications are you currently taking?"),
    "preferred_doctors":     ("preferred_doctors", "Do you have any doctors or specialists you'd need to stay in-network with?"),
    "drug_list":             ("drug_list", "Would you like me to check if your specific medications are covered?"),
}


def build_acquisition_plan(
    lead: dict,
    health_data: dict,
    extra_questions: list[str] | None = None,
) -> list[AcquisitionTask]:
    """
    Returns only missing fields, sorted by priority (1 = first).
    extra_questions are carrier-specific questions injected by the placement engine.
    """
    hd = health_data or {}
    tasks: list[AcquisitionTask] = []

    # 1. Age — every rate calculation depends on it
    if not lead.get("age") and not hd.get("dob"):
        tasks.append(AcquisitionTask(
            field="age",
            priority=1,
            question=(
                "To make sure I find you the best rate, could I get your date of birth, "
                "or your age is fine too?"
            ),
            capture_key="age",
            value_hint="integer years, or date MM/DD/YYYY",
        ))

    # 2. Tobacco — affects every rate band
    tobacco = (hd.get("tobacco") or "unknown").lower()
    if tobacco == "unknown":
        tasks.append(AcquisitionTask(
            field="tobacco",
            priority=2,
            question=(
                "Quick question — have you used any tobacco or nicotine products "
                "in the past 12 months?"
            ),
            capture_key="tobacco",
            value_hint="none | current | former",
        ))

    # 3. Health interview — drives L2 severity scoring
    if not hd.get("conditions") and not hd.get("medications"):
        tasks.append(AcquisitionTask(
            field="health",
            priority=3,
            question=(
                "I have just a few quick health questions so I can find the best option for you. "
                "Have you been diagnosed with or treated for any major health conditions — "
                "things like heart issues, diabetes, cancer, COPD, or kidney disease?"
            ),
            capture_key="conditions",
            value_hint="comma-separated conditions, or 'none'",
        ))

    # 4. Beneficiary — needed to complete the application
    if not hd.get("beneficiary_name"):
        tasks.append(AcquisitionTask(
            field="beneficiary",
            priority=4,
            question=(
                "Who would you like to name as your beneficiary — "
                "the person who would receive the benefit?"
            ),
            capture_key="beneficiary_name",
            value_hint="full name",
        ))

    # 5. Budget — needed to match face value to their range
    if not hd.get("monthly_budget"):
        tasks.append(AcquisitionTask(
            field="budget",
            priority=5,
            question=(
                "What monthly amount would feel comfortable for you? "
                "Most of our plans run between $30 and $150 a month."
            ),
            capture_key="monthly_budget",
            value_hint="dollar amount per month",
        ))

    # Carrier-specific questions injected by the placement engine (priority 6+)
    # Only add if the answer isn't already in health_data
    if extra_questions:
        carrier_priority = 6
        for q_key in extra_questions:
            capture_key, question_text = _CARRIER_QUESTION_TEXT.get(
                q_key, (q_key, f"Could you tell me about your {q_key.replace('_', ' ')}?")
            )
            if not hd.get(capture_key):
                tasks.append(AcquisitionTask(
                    field=f"carrier_{q_key}",
                    priority=carrier_priority,
                    question=question_text,
                    capture_key=capture_key,
                    value_hint="",
                ))
                carrier_priority += 1

    return sorted(tasks, key=lambda t: t.priority)


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
