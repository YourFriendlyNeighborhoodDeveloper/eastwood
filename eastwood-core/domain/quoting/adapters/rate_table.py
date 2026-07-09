from __future__ import annotations

import logging

from sqlalchemy import and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from ..base import CarrierQuote, CarrierQuoteAdapter, QuoteRequest

logger = logging.getLogger(__name__)


class RateTableAdapter(CarrierQuoteAdapter):
    name = "rate_table"
    supported_products = ["final_expense", "life", "medicare_advantage", "medicare_supplement"]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_quotes(self, request: QuoteRequest) -> list[CarrierQuote]:
        from db.models import CarrierRateTable

        if request.age is None:
            return []

        is_medicare = request.product_line in ("medicare_advantage", "medicare_supplement")
        face_value_cents = 0 if is_medicare else (request.face_value or 0)

        clauses = [
            CarrierRateTable.product_line == request.product_line,
            CarrierRateTable.is_active == True,  # noqa: E712
            CarrierRateTable.age == request.age,
        ]
        if not is_medicare:
            clauses.append(CarrierRateTable.tobacco == request.tobacco)
            clauses.append(CarrierRateTable.face_value_cents == face_value_cents)
        if request.state:
            clauses.append(
                or_(CarrierRateTable.state == request.state, CarrierRateTable.state.is_(None))
            )

        result = await self._session.execute(select(CarrierRateTable).where(and_(*clauses)))
        rows = list(result.scalars())

        if not rows:
            return []

        quotes: list[CarrierQuote] = []
        for row in rows:
            premium = row.monthly_premium_cents / 100.0
            quotes.append(
                CarrierQuote(
                    carrier=row.carrier_name,
                    product_line=request.product_line,
                    monthly_premium=round(premium, 2),
                    face_value=face_value_cents if not is_medicare else None,
                    rate_tier=row.rate_tier,
                    source="rate_table",
                )
            )

        return sorted(quotes, key=lambda q: q.monthly_premium)
