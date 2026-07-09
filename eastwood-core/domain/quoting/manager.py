from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .base import CarrierQuote, CarrierQuoteAdapter, QuoteRequest, QuoteResponse

logger = logging.getLogger(__name__)

_SOURCE_PRIORITY = {"cms_api": 3, "rate_table": 2, "formula": 1}


class QuoteManager:
    def __init__(self, adapters: list[CarrierQuoteAdapter], timeout: float = 6.0) -> None:
        self._adapters = adapters
        self._timeout = timeout

    async def get_quotes(self, request: QuoteRequest) -> QuoteResponse:
        eligible = [a for a in self._adapters if a.can_handle(request.product_line, request.state)]
        errors: list[str] = []

        async def _run(adapter: CarrierQuoteAdapter) -> list[CarrierQuote]:
            try:
                return await asyncio.wait_for(adapter.get_quotes(request), timeout=self._timeout)
            except asyncio.TimeoutError:
                errors.append(f"{adapter.name}: timeout")
                return []
            except Exception as exc:
                errors.append(f"{adapter.name}: {exc}")
                logger.exception("adapter %s failed", adapter.name)
                return []

        results = await asyncio.gather(*[_run(a) for a in eligible])

        best_by_key: dict[tuple[str, str], CarrierQuote] = {}
        for batch in results:
            for q in batch:
                key = (q.carrier.lower(), q.rate_tier.lower())
                existing = best_by_key.get(key)
                if existing is None or _SOURCE_PRIORITY.get(q.source, 0) > _SOURCE_PRIORITY.get(existing.source, 0):
                    best_by_key[key] = q

        merged = sorted(
            best_by_key.values(),
            key=lambda q: (_SOURCE_PRIORITY.get(q.source, 0), -q.monthly_premium),
            reverse=True,
        )

        top_source = merged[0].source if merged else "formula"
        return QuoteResponse(
            quotes=merged,
            source=top_source,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            errors=errors,
        )
