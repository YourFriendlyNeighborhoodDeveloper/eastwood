"""Understanding a prospect's text — rules first, an AI model only when needed.

interpret() reads a reply in the context of what Imogen last asked
(`expecting`): a bare "no" answers the tobacco question when that is pending,
a bare "2" picks a time when times were offered. The rules handle the large
majority of replies (yes/no, numbers, amounts, choices, opt-out phrasing,
common objections and questions) at zero cost. Only a reply the rules cannot
place ("unclear") is sent to the optional model (domain.sms.llm), and its
reading is merged under the rules — a model can add understanding, never
override an explicit signal like a decline.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

WORD_NUM = {"five": 5, "ten": 10, "fifteen": 15, "twenty": 20, "twenty five": 25, "twenty-five": 25,
            "thirty": 30, "forty": 40, "fifty": 50}


@dataclass
class Interpretation:
    intents: set[str] = field(default_factory=set)
    values: dict = field(default_factory=dict)
    objection: str | None = None
    question: str | None = None
    question_text: str | None = None
    source: str = "rules"

    @property
    def unclear(self) -> bool:
        return not self.intents and not self.values and not self.objection and not self.question


def _norm(text: str) -> str:
    t = text.lower().replace("’", "'").strip()
    return re.sub(r"\s+", " ", t)


def _has(t: str, *phrases: str) -> bool:
    return any(re.search(rf"(?<![a-z]){re.escape(p)}(?![a-z])", t) for p in phrases)


def _age(t: str, today: date) -> int | None:
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", t)
    if m:
        y = int(m.group(3))
        y = y + (1900 if y > 25 else 2000) if y < 100 else y
        try:
            dob = date(y, int(m.group(1)), int(m.group(2)))
            a = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
            return a if 18 <= a <= 110 else None
        except ValueError:
            return None
    m = re.search(r"\b(?:i'?m|i am|age|aged|turn(?:ed|ing)?)\s+(\d{2,3})\b", t) or \
        re.search(r"\b(\d{2,3})\s*(?:years?|yrs?|y/?o)\b", t)
    if m:
        a = int(m.group(1))
        return a if 18 <= a <= 110 else None
    return None


def _money_values(t: str) -> list[tuple[int, bool]]:
    """(amount, looks_monthly) pairs: '$10,000', '10k', '15 thousand', '$50 a month'."""
    out = []
    for m in re.finditer(r"\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|thousand|grand)?", t):
        raw, mult = m.group(1).replace(",", ""), m.group(2)
        try:
            n = float(raw)
        except ValueError:
            continue
        if mult:
            n *= 1000
        tail = t[m.end():m.end() + 14]
        monthly = bool(re.match(r"\s*(?:a|per|/|each)?\s*(?:mo|month|monthly)", tail))
        out.append((int(round(n)), monthly))
    for word, n in sorted(WORD_NUM.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{word}\s+(?:thousand|k|grand)\b", t):
            out.append((n * 1000, False))
            break  # "twenty five thousand" must not also read as "five thousand"
    return out


def interpret(text: str, *, expecting: str | None = None, offered: int = 0, today: date | None = None) -> Interpretation:
    today = today or date.today()
    t = _norm(text)
    it = Interpretation()
    bare = re.sub(r"[^a-z0-9 ]", "", t).strip()

    if _has(t, "wrong number", "wrong person", "not me", "don't know who", "dont know who"):
        it.intents.add("wrong_number")
        return it
    if _has(t, "not interested", "no thanks", "no thank you", "leave me alone", "don't need", "dont need",
            "not looking", "no longer interested", "do not want", "don't want", "dont want", "quit texting",
            "stop texting", "lose my number", "remove me", "take me off"):
        it.intents.add("decline")
        return it

    if _has(t, "reschedule", "re-schedule", "different time", "another time", "change the time",
            "change my appointment", "move my appointment", "can't make it", "cant make it") or (
            bare == "r" and expecting == "booked"):
        it.intents.add("reschedule")
    if (_has(t, "cancel") or (bare == "c" and expecting == "booked")) and "reschedule" not in it.intents:
        it.intents.add("cancel")
    if _has(t, "call me", "give me a call", "can you call", "could you call", "phone call", "talk to someone",
            "speak to someone", "talk to a person", "speak with someone", "real person", "call instead"):
        it.intents.add("wants_call")
        if _has(t, "now", "right now", "asap", "today"):
            it.values["call_now"] = True

    # objections
    if _has(t, "too expensive", "too much", "can't afford", "cant afford", "cheaper", "lower price",
            "less expensive", "out of my budget", "costs too"):
        it.objection = "price"
    elif _has(t, "my wife", "my husband", "my spouse", "talk to my", "ask my", "check with my", "discuss with"):
        it.objection = "spouse"
    elif _has(t, "already have", "already covered", "have insurance", "have a policy", "got insurance"):
        it.objection = "have_coverage"
    elif _has(t, "scam", "is this real", "legit", "fraud", "spam"):
        it.objection = "trust"
    elif _has(t, "busy", "later", "not now", "not right now", "next week", "some other time", "at work"):
        it.objection = "not_now"

    # questions
    if "?" in t or re.match(r"^(who|what|how|when|why|is|are|do|does|can|will|would)\b", t):
        if _has(t, "who is this", "who are you", "who's this", "whos this", "who is texting"):
            it.question = "who"
        elif _has(t, "how did you get", "where did you get", "how'd you get"):
            it.question = "how_got_number"
        elif _has(t, "are you a bot", "are you a robot", "are you real", "are you ai", "is this ai",
                  "is this a robot", "real person"):
            it.question = "is_ai"
        elif _has(t, "exam", "physical", "blood test", "waiting period", "wait period"):
            it.question = "exam"
        elif _has(t, "what does it cover", "what is final expense", "what does final expense", "what kind of",
                  "what is this", "how does it work", "what does this cover"):
            it.question = "what_covers"
        elif _has(t, "how much", "what would it cost", "the cost", "price", "rate", "premium"):
            it.question = "cost"
        elif it.objection != "trust":
            it.question = "other"
        it.question_text = text.strip()[:300]

    # values, read in context
    a = _age(t, today)
    if a is not None:
        it.values["age"] = a
    if _has(t, "male", "man", "guy") and not _has(t, "female", "woman"):
        it.values["sex"] = "M"
    elif _has(t, "female", "woman", "lady"):
        it.values["sex"] = "F"
    if expecting == "sex" and bare in ("m", "f"):
        it.values["sex"] = bare.upper()
    if re.search(r"\b(non-?smoker|nonsmoker)\b", t):
        it.values["tobacco"] = False
    elif re.search(r"\b(smoke[sd]?|smoker|smoking|tobacco|cigarettes?|cigars?|chew|vape|vaping|nicotine|dip)\b", t):
        if re.search(r"\b(quit|stopped)\b", t):
            # Quit more than a year ago is non-tobacco for most carriers; anything
            # shorter or unspecified stays unanswered and is asked again.
            if re.search(r"\b(years?|yrs?)\b", t) and not re.search(r"\b(months?|weeks?|days?)\b", t):
                it.values["tobacco"] = False
        elif re.search(r"\b(don'?t|do not|never|no|non|not)\b", t):
            it.values["tobacco"] = False
        else:
            it.values["tobacco"] = True
    for word in ("burial", "cremation"):
        if _has(t, word, "buried" if word == "burial" else "cremated"):
            it.values["purpose"] = word

    t_amounts = re.sub(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", " ", t)  # dates are not amounts
    for amount, monthly in _money_values(t_amounts):
        if a is not None and amount == a and not monthly:
            continue  # "I'm 67" is an age, not $67,000 of coverage
        if monthly or (expecting == "budget" and amount < 1000):
            it.values.setdefault("budget", amount)
        elif amount >= 1000:
            it.values.setdefault("face", amount)
        elif expecting == "age" and 18 <= amount <= 110 and "age" not in it.values:
            it.values["age"] = amount
        elif expecting == "face" and 1 <= amount <= 100:
            it.values.setdefault("face", amount * 1000)  # "15" -> 15,000
    if expecting in ("time", "option") and re.fullmatch(r"(option )?([1-9])", bare):
        n = int(re.fullmatch(r"(?:option )?([1-9])", bare).group(1))
        if expecting == "time" and n <= max(offered, 0):
            it.values["time_choice"] = n
        elif expecting == "option":
            it.values["option_choice"] = n
        it.values.pop("age", None)
    for word, n in (("first", 1), ("second", 2), ("third", 3)):
        if expecting in ("time", "option") and _has(t, f"the {word}", f"{word} one", f"{word} option"):
            it.values["time_choice" if expecting == "time" else "option_choice"] = n

    yes = bare in ("y", "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "yes please",
                   "sounds good", "absolutely", "of course", "definitely", "go ahead", "interested") or \
        bool(re.match(r"^(yes|yeah|yep|sure|ok|okay)\b", bare)) or _has(t, "i'm interested", "im interested")
    no = bare in ("n", "no", "nope", "nah", "no sir", "no maam", "not really", "none", "never") or \
        bool(re.match(r"^(no|nope|nah)\b", bare))
    if expecting == "tobacco" and "tobacco" not in it.values and (yes or no):
        it.values["tobacco"] = yes
    elif expecting == "budget" and no and "budget" not in it.values:
        it.values["budget"] = 0  # explicitly no budget
    elif expecting and expecting.startswith("health:") and (yes or no):
        it.values[expecting] = yes
    elif expecting == "face" and _has(t, "not sure", "no idea", "don't know", "dont know", "whatever", "normal",
                                      "recommend", "you tell me", "typical"):
        it.values["face_unsure"] = True
    elif yes:
        it.intents.add("yes")
    elif no:
        it.intents.add("no")
    if _has(t, "thank", "thanks", "thx", "great", "awesome", "perfect", "see you") and not it.values:
        it.intents.add("thanks")
    return it


def merge(rules: Interpretation, model: Interpretation | None) -> Interpretation:
    """Model reading fills gaps; explicit rule signals always win."""
    if model is None:
        return rules
    merged = Interpretation(intents=set(rules.intents) | set(model.intents), values={**model.values, **rules.values},
                            objection=rules.objection or model.objection, question=rules.question or model.question,
                            question_text=rules.question_text or model.question_text,
                            source="rules" if not rules.unclear else "rules+model")
    if "decline" in rules.intents or "wrong_number" in rules.intents:
        merged.intents = set(rules.intents)
    return merged
