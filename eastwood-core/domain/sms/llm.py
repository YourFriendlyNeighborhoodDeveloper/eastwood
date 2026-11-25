"""Optional model reader for free-text replies the rules cannot place.

SMS_LLM_PROVIDER=openai sends ONLY those replies (with the question Imogen
asked) to an economical chat model and asks for a structured reading — never
for text to send. Output is constrained by a strict JSON schema; usage is
metered into the spend ledger (kind "llm") at SMS_LLM_COST_* prices.

Privacy: the prospect's message text goes to the model provider. Enable it
only after reviewing that provider's data terms; with the default (none),
nothing leaves the stack.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx

from config.settings import get_settings

from .nlu import Interpretation

logger = logging.getLogger(__name__)

_INTENTS = ["yes", "no", "decline", "wrong_number", "wants_call", "reschedule", "cancel", "thanks"]
_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["intents", "age", "face", "budget", "tobacco", "sex", "purpose", "objection", "question",
                 "time_choice", "option_choice", "health_answer"],
    "properties": {
        "intents": {"type": "array", "items": {"type": "string", "enum": _INTENTS}},
        "age": {"type": ["integer", "null"]},
        "face": {"type": ["integer", "null"], "description": "coverage amount in dollars"},
        "budget": {"type": ["integer", "null"], "description": "monthly budget in dollars"},
        "tobacco": {"type": ["boolean", "null"]},
        "sex": {"type": ["string", "null"], "enum": ["M", "F", None]},
        "purpose": {"type": ["string", "null"], "enum": ["burial", "cremation", None]},
        "objection": {"type": ["string", "null"], "enum": ["price", "spouse", "have_coverage", "trust", "not_now", None]},
        "question": {"type": ["string", "null"],
                     "enum": ["who", "how_got_number", "is_ai", "exam", "what_covers", "cost", "other", None]},
        "time_choice": {"type": ["integer", "null"]},
        "option_choice": {"type": ["integer", "null"]},
        "health_answer": {"type": ["boolean", "null"]},
    },
}
_SYSTEM = (
    "You read one text message from an insurance prospect and return what it says, as JSON. "
    "Only extract what the message actually states; use null when it does not say. Never guess an age, "
    "amount or answer. 'expecting' tells you which question the prospect is answering."
)


@dataclass
class ModelReading:
    interpretation: Interpretation
    tokens_in: int
    tokens_out: int
    cost_usd: float


async def read_with_model(text: str, *, expecting: str | None, last_question: str | None) -> ModelReading | None:
    s = get_settings()
    if s.sms_llm_provider.strip().lower() != "openai" or not s.openai_api_key:
        return None
    payload = {
        "model": s.sms_llm_model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps({"expecting": expecting, "question_asked": last_question,
                                                    "message": text[:600]})},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "sms_reading", "strict": True,
                                                                   "schema": _SCHEMA}},
    }
    if s.sms_llm_model.startswith("gpt-5"):
        payload["reasoning_effort"] = "minimal"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post("https://api.openai.com/v1/chat/completions", json=payload,
                                  headers={"Authorization": f"Bearer {s.openai_api_key}"})
        r.raise_for_status()
        data = r.json()
        obj = json.loads(data["choices"][0]["message"]["content"])
    except Exception:
        logger.warning("SMS model reading failed; continuing with rules only", exc_info=True)
        return None
    usage = data.get("usage") or {}
    t_in, t_out = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
    cost = t_in / 1e6 * s.sms_llm_cost_in_per_m_usd + t_out / 1e6 * s.sms_llm_cost_out_per_m_usd
    it = Interpretation(intents=set(obj.get("intents") or []), objection=obj.get("objection"),
                        question=obj.get("question"), source="model")
    for key in ("age", "face", "budget", "tobacco", "sex", "purpose", "time_choice", "option_choice"):
        if obj.get(key) is not None:
            it.values[key] = obj[key]
    if obj.get("health_answer") is not None and expecting and expecting.startswith("health:"):
        it.values[expecting] = obj["health_answer"]
    if it.question:
        it.question_text = text.strip()[:300]
    return ModelReading(it, t_in, t_out, round(cost, 6))
