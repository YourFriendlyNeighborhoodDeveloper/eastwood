from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from ...core.ids import new_id
from ..postgres import PostgresStore

class BookingRepo:
    def __init__(self, pg: PostgresStore):
        self._pg = pg

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT token, lead_id, created_at, expires_at FROM booking_tokens WHERE token=%s",
                    (token,),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"token": row[0], "lead_id": row[1], "created_at": row[2], "expires_at": row[3]}

    def get_active_for_lead(self, lead_id: str) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc)
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT token, lead_id, created_at, expires_at FROM booking_tokens WHERE lead_id=%s AND expires_at>%s ORDER BY created_at DESC LIMIT 1",
                    (lead_id, now),
                )
                row = cur.fetchone()
        if not row:
            return None
        return {"token": row[0], "lead_id": row[1], "created_at": row[2], "expires_at": row[3]}

    def create(self, lead_id: str, ttl_hours: int) -> Dict[str, Any]:
        token = new_id()
        now = datetime.now(timezone.utc)
        exp = now + timedelta(hours=int(ttl_hours))
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO booking_tokens(token, lead_id, expires_at) VALUES (%s,%s,%s)",
                    (token, lead_id, exp),
                )
            conn.commit()
        return {"token": token, "lead_id": lead_id, "expires_at": exp}

    def get_or_create(self, lead_id: str, ttl_hours: int) -> Dict[str, Any]:
        cur = self.get_active_for_lead(lead_id)
        if cur:
            return cur
        return self.create(lead_id, ttl_hours)