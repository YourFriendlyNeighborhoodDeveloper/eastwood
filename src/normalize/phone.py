from typing import Optional, Tuple
import phonenumbers


def to_e164(raw: Optional[str], default_region: str = "US") -> Tuple[Optional[str], Optional[str]]:
    if raw is None:
        return None, "missing"
    s = str(raw).strip()
    if s == "":
        return None, "missing"
    try:
        p = phonenumbers.parse(s, default_region)
        if not phonenumbers.is_possible_number(p):
            return None, "impossible"
        if not phonenumbers.is_valid_number(p):
            return None, "invalid"
        return phonenumbers.format_number(p, phonenumbers.PhoneNumberFormat.E164), None
    except Exception:
        return None, "parse_error"