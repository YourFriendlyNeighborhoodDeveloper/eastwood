from __future__ import annotations

from ..base import CarrierQuote, CarrierQuoteAdapter, QuoteRequest


class RateSheetAdapter(CarrierQuoteAdapter):
    """Exact premiums from verified carrier rate sheets, for the QuoteManager
    stack (/api/quote). See domain.quoting.rate_sheets for the rules."""

    name = "rate_sheet"
    supported_products = ["final_expense", "life"]

    def __init__(self, session, tenant_id: str, allow_test: bool = False) -> None:
        self._session = session
        self._tenant = tenant_id
        self._allow_test = allow_test

    async def get_quotes(self, request: QuoteRequest) -> list[CarrierQuote]:
        from ..rate_sheets import quote_from_sheets

        sex = (request.gender or "").strip().upper()[:1] or None
        quotes, _ = await quote_from_sheets(
            self._session, tenant_id=self._tenant, product_line=request.product_line, state=request.state,
            age=request.age, sex=sex if sex in ("M", "F") else None, tobacco=request.tobacco,
            face=int((request.face_value or 0) / 100), tier=request.rate_tier, mode="monthly",
            allow_test=self._allow_test)
        return [CarrierQuote(
            carrier=q.carrier, product_line=q.product_line, monthly_premium=float(q.premium),
            face_value=q.face * 100, rate_tier=q.tier, plan_name=q.plan_name, plan_id=q.sheet_id,
            source="rate_sheet_test" if q.is_test else "rate_sheet", raw_data=q.provenance(),
        ) for q in quotes]
