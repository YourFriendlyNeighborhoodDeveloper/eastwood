from typing import Any, Dict
from psycopg_pool import ConnectionPool


class PostgresStore:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = ConnectionPool(conninfo=dsn, min_size=1, max_size=10, open=True)

    @staticmethod
    def from_cfg(cfg: Dict[str, Any]) -> "PostgresStore":
        host = cfg["host"]
        port = int(cfg["port"])
        db = cfg["db"]
        user = cfg["user"]
        password = cfg["password"]
        sslmode = cfg.get("sslmode", "disable")
        dsn = f"postgresql://{user}:{password}@{host}:{port}/{db}?sslmode={sslmode}"
        return PostgresStore(dsn)

    def close(self) -> None:
        self.pool.close()