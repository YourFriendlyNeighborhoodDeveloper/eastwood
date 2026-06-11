from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.errors import KafkaError

from config.settings import get_settings

logger = logging.getLogger(__name__)

_producer: AIOKafkaProducer | None = None


async def get_producer() -> AIOKafkaProducer:
    global _producer
    if _producer is None:
        settings = get_settings()
        _producer = AIOKafkaProducer(
            bootstrap_servers=settings.kafka_brokers,
            enable_idempotence=True,
            compression_type="gzip",
            linger_ms=10,
            value_serializer=lambda v: orjson.dumps(v),
            key_serializer=lambda k: k.encode() if k else None,
        )
        await _producer.start()
    return _producer


async def stop_producer() -> None:
    global _producer
    if _producer is not None:
        await _producer.stop()
        _producer = None


async def publish(topic: str, value: dict, key: str | None = None) -> None:
    producer = await get_producer()
    await producer.send_and_wait(topic, value=value, key=key)


class KafkaConsumer:
    def __init__(self, group_id: str, topics: list[str]) -> None:
        self._group_id = group_id
        self._topics = topics
        self._handlers: dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._consumer: AIOKafkaConsumer | None = None
        self._running = False

    def on(self, topic: str):
        def decorator(fn: Callable[[dict], Awaitable[None]]):
            self._handlers[topic] = fn
            return fn
        return decorator

    async def start(self) -> None:
        settings = get_settings()
        self._consumer = AIOKafkaConsumer(
            *self._topics,
            bootstrap_servers=settings.kafka_brokers,
            group_id=self._group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda v: orjson.loads(v),
        )
        await self._consumer.start()
        self._running = True
        asyncio.create_task(self._consume_loop())

    async def stop(self) -> None:
        self._running = False
        if self._consumer:
            await self._consumer.stop()

    async def _consume_loop(self) -> None:
        assert self._consumer is not None
        settings = get_settings()
        async for msg in self._consumer:
            if not self._running:
                break
            handler = self._handlers.get(msg.topic)
            if handler is None:
                await self._consumer.commit()
                continue
            try:
                await handler(msg.value)
                await self._consumer.commit()
            except Exception as exc:
                logger.exception("consumer error on topic %s: %s", msg.topic, exc)
                try:
                    await publish(
                        settings.topic_dlq,
                        {
                            "topic": msg.topic,
                            "key": msg.key.decode("utf-8") if isinstance(msg.key, bytes) else msg.key,
                            "value": msg.value if isinstance(msg.value, dict) else str(msg.value),
                            "error": str(exc),
                        },
                    )
                except Exception:
                    logger.exception("failed to route to DLQ")
                await self._consumer.commit()
