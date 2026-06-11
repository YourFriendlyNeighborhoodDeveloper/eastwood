from datetime import datetime, timezone
from typing import Any, Dict
from ..postgres import PostgresStore

class ConsentRepo:
    def __init__(self, pg: PostgresStore):
        self._pg = pg

    def insert(self, consent_id: str, lead_id: str, ts_iso: str, evidence: Dict[str, Any]) -> None:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO consent_ledger(consent_id, lead_id, ts, evidence) VALUES (%s,%s,%s,%s::jsonb) "
                    "ON CONFLICT (lead_id) DO NOTHING",
                    (consent_id, lead_id, ts_iso, self._json(evidence)),
                )
            conn.commit()

    def exists_for_lead(self, lead_id: str) -> bool:
        with self._pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM consent_ledger WHERE lead_id=%s LIMIT 1", (lead_id,))
                r = cur.fetchone()
        return r is not None

    def _json(self, obj: Dict[str, Any]) -> str:
        import json
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)