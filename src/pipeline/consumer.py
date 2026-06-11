import json
import logging
from typing import Callable, Dict, Any, Optional
from kafka import KafkaConsumer

log = logging.getLogger("eastwood.consumer")

Handler = Callable[[Dict[str, Any]], None]

class ConsumerLoop:
    def __init__(
        self,
        bootstrap_servers: str,
        topic: str,
        group_id: str,
        handler: Handler,
        auto_offset_reset: str = "latest",
        enable_auto_commit: bool = True,
    ):
        self._consumer = KafkaConsumer(
            topic,
            bootstrap_servers=bootstrap_servers,
            group_id=group_id,
            auto_offset_reset=auto_offset_reset,
            enable_auto_commit=enable_auto_commit,
            value_deserializer=lambda b: b.decode("utf-8"),
        )
        self._handler = handler
        self._topic = topic

    def run(self, stop_flag: Optional[Callable[[], bool]] = None) -> None:
        for msg in self._consumer:
            if stop_flag and stop_flag():
                break
            try:
                payload = json.loads(msg.value)
                self._handler(payload)
            except Exception:
                log.exception("handler failed topic=%s", self._topic)