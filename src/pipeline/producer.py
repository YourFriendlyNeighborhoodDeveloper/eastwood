import json
from typing import Any, Dict
from kafka import KafkaProducer

class Producer:
    def __init__(self, bootstrap_servers: str, client_id: str = "eastwood"):
        self._p = KafkaProducer(
            bootstrap_servers=bootstrap_servers,
            client_id=client_id,
            value_serializer=lambda v: json.dumps(v, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )

    def send(self, topic: str, msg: Dict[str, Any]) -> None:
        self._p.send(topic, msg)

    def flush(self) -> None:
        self._p.flush()