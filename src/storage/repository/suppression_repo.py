import hashlib
from typing import Optional

from ..postgres import PostgresStore
from ..redis import RedisStore


class SuppressionRepo:
    def __init__(self, pg: PostgresStore, redis: RedisStore):
        self.pg = pg
        self.redis = redis

    def is_suppressed_phone(self, phone_e164: str) -> bool:
        k = self._cache_key("phone", phone_e164)
        cached = self.redis.client.get(k)
        if cached is not None:
            return cached == "1"

        with self.pg.pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM suppressions WHERE kind=%s AND value=%s LIMIT 1",
                    ("phone", phone_e164),
                )
                row = cur.fetchone()
                val = "1" if row else "0"
                self.redis.client.setex(k, 300, val)
                return val == "1"

    def dedupe_seen_recently(self, product: str, phone_e164: str, window_seconds: int) -> bool:
        key = self._cache_key("dedupe", f"{product}:{phone_e164}")
        added = self.redis.client.set(key, "1", nx=True, ex=window_seconds)
        return added is None

    def _cache_key(self, kind: str, value: str) -> str:
        h = hashlib.sha256(f"{kind}:{value}".encode("utf-8")).hexdigest()
        return f"eastwood:{kind}:{h}"