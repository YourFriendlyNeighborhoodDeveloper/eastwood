from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from ...core.ids import new_id
from ..postgres import PostgresStore

class AppointmentsRepo:
    def __init__(self, pg: PostgresStore):
        self._pg = pg

    def is_slot_available(self, start_at_utc: datetime) -> bool:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM appointments WHERE start_at=%s AND status='booked' LIMIT 1",
                    (start_at_utc,),
                )
                row = cur.fetchone()
        return row is None

    def create(self, lead_id: str, start_at: datetime, tz: str) -> str:
        appt_id = new_id()
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO appointments(appointment_id, lead_id, start_at, tz, status) VALUES (%s,%s,%s,%s,'booked')",
                    (appt_id, lead_id, start_at, tz),
                )
            conn.commit()
        return appt_id

    def list_booked_between(self, start_utc: datetime, end_utc: datetime) -> List[Dict[str, Any]]:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT appointment_id, lead_id, start_at, tz, status, created_at, confirm_sent_at, reminder_24h_sent_at, reminder_2h_sent_at "
                    "FROM appointments WHERE start_at>=%s AND start_at<%s AND status='booked' ORDER BY start_at ASC",
                    (start_utc, end_utc),
                )
                rows = cur.fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "appointment_id": r[0],
                    "lead_id": r[1],
                    "start_at": r[2],
                    "tz": r[3],
                    "status": r[4],
                    "created_at": r[5],
                    "confirm_sent_at": r[6],
                    "reminder_24h_sent_at": r[7],
                    "reminder_2h_sent_at": r[8],
                }
            )
        return out

    def mark_confirm_sent(self, appointment_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE appointments SET confirm_sent_at=%s WHERE appointment_id=%s",
                    (now, appointment_id),
                )
            conn.commit()

    def mark_reminder_24h_sent(self, appointment_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE appointments SET reminder_24h_sent_at=%s WHERE appointment_id=%s",
                    (now, appointment_id),
                )
            conn.commit()

    def mark_reminder_2h_sent(self, appointment_id: str) -> None:
        now = datetime.now(timezone.utc)
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE appointments SET reminder_2h_sent_at=%s WHERE appointment_id=%s",
                    (now, appointment_id),
                )
            conn.commit()

    def list_booked_with_lead_between(self, start_utc: datetime, end_utc: datetime) -> List[Dict[str, Any]]:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT a.appointment_id, a.lead_id, a.start_at, a.tz, a.confirm_sent_at, a.reminder_24h_sent_at, a.reminder_2h_sent_at, "
                    "l.product, l.phone_e164, l.email, l.state, l.zip, l.payload "
                    "FROM appointments a JOIN leads l ON l.lead_id=a.lead_id "
                    "WHERE a.start_at>=%s AND a.start_at<%s AND a.status='booked' ORDER BY a.start_at ASC",
                    (start_utc, end_utc),
                )
                rows = cur.fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "appointment_id": r[0],
                    "lead_id": r[1],
                    "start_at": r[2],
                    "tz": r[3],
                    "confirm_sent_at": r[4],
                    "reminder_24h_sent_at": r[5],
                    "reminder_2h_sent_at": r[6],
                    "product": r[7],
                    "phone_e164": r[8],
                    "email": r[9],
                    "state": r[10],
                    "zip": r[11],
                    "payload": r[12] or {},
                }
            )
        return out