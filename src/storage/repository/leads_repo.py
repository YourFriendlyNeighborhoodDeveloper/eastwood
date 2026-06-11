from datetime import datetime
from typing import Any, Dict, Optional
from ..postgres import PostgresStore

class LeadsRepo:
    def __init__(self, pg: PostgresStore):
        self._pg = pg

    def upsert(self, lead: Dict[str, Any], status: str) -> None:
        lead_id = str(lead["lead_id"])
        created_at = str(lead["created_at"])
        source = str(lead.get("source") or "unknown")
        product = str(lead.get("product") or "")
        phone = str(lead.get("phone_e164") or "")
        email = lead.get("email")
        state = lead.get("state")
        zipc = lead.get("zip")
        payload = lead.get("payload") if isinstance(lead.get("payload"), dict) else {}

        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO leads(lead_id, created_at, source, product, phone_e164, email, state, zip, payload, status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
                    "ON CONFLICT (lead_id) DO UPDATE SET "
                    "source=EXCLUDED.source, product=EXCLUDED.product, phone_e164=EXCLUDED.phone_e164, email=EXCLUDED.email, "
                    "state=EXCLUDED.state, zip=EXCLUDED.zip, payload=EXCLUDED.payload, status=EXCLUDED.status, updated_at=now()",
                    (lead_id, created_at, source, product, phone, email, state, zipc, self._json(payload), status),
                )
            conn.commit()

    def set_status(self, lead_id: str, status: str) -> None:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE leads SET status=%s, updated_at=now() WHERE lead_id=%s", (status, lead_id))
            conn.commit()

    def set_score(self, lead_id: str, score: int) -> None:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE leads SET score=%s, updated_at=now() WHERE lead_id=%s", (int(score), lead_id))
            conn.commit()

    def set_routed(self, lead_id: str, routed_to: str) -> None:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE leads SET routed_to=%s, updated_at=now() WHERE lead_id=%s", (routed_to, lead_id))
            conn.commit()

    def merge_payload(self, lead_id: str, patch: Dict[str, Any]) -> None:
        if not isinstance(patch, dict) or not patch:
            return
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET payload = COALESCE(payload,'{}'::jsonb) || %s::jsonb, updated_at=now() WHERE lead_id=%s",
                    (self._json(patch), lead_id),
                )
            conn.commit()

    def get(self, lead_id: str) -> Optional[Dict[str, Any]]:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT lead_id, created_at, source, product, phone_e164, email, state, zip, payload, status, updated_at, score, routed_to "
                    "FROM leads WHERE lead_id=%s",
                    (lead_id,),
                )
                r = cur.fetchone()
        if not r:
            return None
        return {
            "lead_id": r[0],
            "created_at": r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]),
            "source": r[2],
            "product": r[3],
            "phone_e164": r[4],
            "email": r[5],
            "state": r[6],
            "zip": r[7],
            "payload": r[8] or {},
            "status": r[9],
            "updated_at": r[10].isoformat() if hasattr(r[10], "isoformat") else str(r[10]),
            "score": r[11],
            "routed_to": r[12],
        }

    def _json(self, obj: Dict[str, Any]) -> str:
        import json
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)