from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo
from typing import Any, Dict, List

def generate_slots(now_utc: datetime, days: int, work_hours: Dict[str, Any], booked_utc: List[datetime]) -> List[datetime]:
    tz = ZoneInfo(str(work_hours.get("tz") or "America/Detroit"))
    days_allowed = set(int(x) for x in (work_hours.get("days") or [1, 2, 3, 4, 5]))
    start_str = str(work_hours.get("start") or "10:00")
    end_str = str(work_hours.get("end") or "18:00")
    slot_minutes = int(work_hours.get("slot_minutes") or 20)
    buffer_minutes = int(work_hours.get("buffer_minutes") or 10)

    sh, sm = (int(x) for x in start_str.split(":"))
    eh, em = (int(x) for x in end_str.split(":"))
    start_t = time(sh, sm)
    end_t = time(eh, em)

    booked_set = set(dt.replace(second=0, microsecond=0) for dt in booked_utc)

    out: List[datetime] = []
    now_local = now_utc.astimezone(tz)

    for d in range(0, int(days) + 1):
        day = (now_local + timedelta(days=d)).date()
        weekday = (now_local + timedelta(days=d)).isoweekday()
        if weekday not in days_allowed:
            continue

        day_start_local = datetime.combine(day, start_t, tzinfo=tz)
        day_end_local = datetime.combine(day, end_t, tzinfo=tz)

        cursor = max(day_start_local, now_local + timedelta(minutes=buffer_minutes))
        cursor = cursor.replace(second=0, microsecond=0)

        while cursor + timedelta(minutes=slot_minutes) <= day_end_local:
            slot_utc = cursor.astimezone(timezone.utc).replace(second=0, microsecond=0)
            if slot_utc > now_utc and slot_utc not in booked_set:
                out.append(slot_utc)
            cursor = cursor + timedelta(minutes=slot_minutes + buffer_minutes)

    out.sort()
    return out[:200]