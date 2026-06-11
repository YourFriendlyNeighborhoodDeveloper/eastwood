from datetime import datetime, timezone

def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)

def now_iso() -> str:
    return now_utc().isoformat()