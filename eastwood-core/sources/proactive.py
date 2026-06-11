from __future__ import annotations

import asyncio
import logging

import httpx

from config.settings import get_settings

logger = logging.getLogger(__name__)

_FEC_BASE = "https://api.open.fec.gov/v1"
_NPI_BASE = "https://npiregistry.cms.hhs.gov/api"


class FecClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._api_key = get_settings().openfec_api_key

    async def lookup_donor(self, name: str, state: str | None = None) -> bool:
        if not self._api_key:
            return False
        try:
            params: dict = {"api_key": self._api_key, "q": name, "per_page": 1}
            if state:
                params["contributor_state"] = state
            resp = await self._client.get(f"{_FEC_BASE}/schedules/schedule_a/", params=params, timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            return data.get("pagination", {}).get("count", 0) > 0
        except Exception as exc:
            logger.warning("FEC lookup failed: %s", exc)
            return False


class NpiClient:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def lookup_provider(self, first_name: str, last_name: str, state: str | None = None) -> bool:
        try:
            params: dict = {
                "version": "2.1",
                "first_name": first_name,
                "last_name": last_name,
                "limit": 1,
            }
            if state:
                params["state"] = state
            resp = await self._client.get(_NPI_BASE, params=params, timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            return int(data.get("result_count", 0)) > 0
        except Exception as exc:
            logger.warning("NPI lookup failed: %s", exc)
            return False


async def enrich_lead(
    first_name: str | None,
    last_name: str | None,
    state: str | None,
) -> dict:
    async with httpx.AsyncClient() as client:
        fec = FecClient(client)
        npi = NpiClient(client)

        name = f"{first_name or ''} {last_name or ''}".strip()
        fec_result, npi_result = await asyncio.gather(
            fec.lookup_donor(name, state) if name else asyncio.sleep(0, result=False),
            npi.lookup_provider(first_name or "", last_name or "", state)
            if first_name and last_name
            else asyncio.sleep(0, result=False),
        )

    return {
        "fec_donor": fec_result,
        "npi_provider": npi_result,
    }
