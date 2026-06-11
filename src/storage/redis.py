from typing import Any, Dict
import redis


class RedisStore:
    def __init__(self, host: str, port: int, db: int):
        self.client = redis.Redis(host=host, port=port, db=db, decode_responses=True)

    @staticmethod
    def from_cfg(cfg: Dict[str, Any]) -> "RedisStore":
        return RedisStore(
            host=str(cfg["host"]),
            port=int(cfg["port"]),
            db=int(cfg.get("db", 0)),
        )