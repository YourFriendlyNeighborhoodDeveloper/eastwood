import os
from typing import Iterable
from .postgres import PostgresStore

def _sql_files() -> Iterable[str]:
    base = "migrations/postgres"
    names = [
        "0001_init.sql",
        "0002_indexes.sql",
        "0003_leads_status.sql",
          "0005_leads_contact.sql",
          "0006_leads_payload.sql",
        "0004_booking.sql",
    ]
    for n in names:
        yield os.path.join(base, n)

def apply_migrations(pg: PostgresStore) -> None:
    for path in _sql_files():
        if not os.path.exists(path):
            continue
        with open(path, "r") as f:
            sql = f.read().strip()
        if not sql:
            continue
        with pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
            conn.commit()