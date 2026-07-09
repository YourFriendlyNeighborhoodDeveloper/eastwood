from __future__ import annotations

import logging

import httpx

from ..base import CarrierQuote, CarrierQuoteAdapter, QuoteRequest

logger = logging.getLogger(__name__)

CMS_DATA_API_BASE = "https://data.cms.gov/data-api/v1/dataset"
_PLACEHOLDER_IDS: frozenset[str] = frozenset({"pjlv-rqvd", "", "none"})


class CmsMedicareAdapter(CarrierQuoteAdapter):
    name = "cms_medicare"
    supported_products = ["medicare_advantage", "medicare_supplement"]

    def __init__(
        self,
        ma_dataset_id: str = "pjlv-rqvd",
        ms_dataset_id: str | None = None,
        target_carriers: list[str] | None = None,
        timeout: float = 6.0,
    ) -> None:
        self._ma_id = ma_dataset_id
        self._ms_id = ms_dataset_id
        self._target = {c.lower() for c in (target_carriers or [])}
        self._timeout = timeout

    async def get_quotes(self, request: QuoteRequest) -> list[CarrierQuote]:
        if request.product_line == "medicare_advantage":
            return await self._fetch_ma(request)
        if request.product_line == "medicare_supplement":
            return await self._fetch_ms(request)
        return []

    async def _fetch_ma(self, request: QuoteRequest) -> list[CarrierQuote]:
        if self._ma_id.lower() in _PLACEHOLDER_IDS:
            return []
        url = f"{CMS_DATA_API_BASE}/{self._ma_id}/data"
        params: dict[str, str | int] = {"size": 50}
        if request.state:
            params["filter[State_Code]"] = request.state
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    logger.warning("CMS MA API returned %d", resp.status_code)
                    return []
                data = resp.json()
        except Exception as exc:
            logger.warning("CMS MA API error: %s", exc)
            return []

        quotes: list[CarrierQuote] = []
        rows = data if isinstance(data, list) else data.get("data", [])
        for row in rows:
            carrier = str(row.get("Organization_Name", row.get("organization_name", ""))).strip()
            if not carrier:
                continue
            if self._target and not any(t in carrier.lower() for t in self._target):
                continue
            premium_raw = row.get("Premium_Amount", row.get("premium_amount", row.get("Part_C_Premium", 0)))
            try:
                premium = float(premium_raw or 0)
            except (ValueError, TypeError):
                continue
            plan_name = str(row.get("Plan_Name", row.get("plan_name", "")))
            plan_id = str(row.get("Contract_ID", row.get("contract_id", "")))
            quotes.append(
                CarrierQuote(
                    carrier=carrier,
                    product_line="medicare_advantage",
                    monthly_premium=premium,
                    face_value=None,
                    rate_tier="ma_plan",
                    plan_name=plan_name or None,
                    plan_id=plan_id or None,
                    source="cms_api",
                    raw_data=row,
                )
            )
        return quotes[:10]

    async def _fetch_ms(self, request: QuoteRequest) -> list[CarrierQuote]:
        if not self._ms_id or self._ms_id.lower() in _PLACEHOLDER_IDS:
            return []
        url = f"{CMS_DATA_API_BASE}/{self._ms_id}/data"
        params: dict[str, str | int] = {"size": 50}
        if request.state:
            params["filter[State_Code]"] = request.state
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, params=params)
                if resp.status_code != 200:
                    return []
                data = resp.json()
        except Exception as exc:
            logger.warning("CMS MS API error: %s", exc)
            return []

        quotes: list[CarrierQuote] = []
        rows = data if isinstance(data, list) else data.get("data", [])
        for row in rows:
            carrier = str(row.get("Company_Name", row.get("company_name", ""))).strip()
            if not carrier:
                continue
            if self._target and not any(t in carrier.lower() for t in self._target):
                continue
            premium_raw = row.get("Monthly_Premium", row.get("monthly_premium", 0))
            try:
                premium = float(premium_raw or 0)
            except (ValueError, TypeError):
                continue
            plan_letter = str(row.get("Plan_Type", row.get("plan_type", "G")))
            quotes.append(
                CarrierQuote(
                    carrier=carrier,
                    product_line="medicare_supplement",
                    monthly_premium=premium,
                    face_value=None,
                    rate_tier=f"plan_{plan_letter.lower()}",
                    plan_name=f"Plan {plan_letter}" if plan_letter else None,
                    source="cms_api",
                    raw_data=row,
                )
            )
        return quotes[:10]
