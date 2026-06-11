from __future__ import annotations

import httpx

from config.settings import get_settings


class AiAgentClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.ai_agent_base_url
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=10.0)

    async def start_session(self, lead_id: str, context: dict) -> dict:
        enriched_context = {
            **context,
            # Explicit camelCase mappings — Zod schema on the TS side uses camelCase
            "tenantId":       context.get("tenant_id", "default"),
            "phoneE164":      context.get("phone_e164", ""),
            "firstName":      context.get("first_name"),
            "lastName":       context.get("last_name"),
            "productLine":    context.get("product_line"),
            "triageBucket":   context.get("triage_bucket"),
            "acquisitionPlan": context.get("acquisition_plan", []),
        }
        resp = await self._client.post(
            "/sessions",
            json={"lead_id": lead_id, "context": enriched_context},
        )
        resp.raise_for_status()
        return resp.json()

    async def end_session(self, session_id: str) -> None:
        resp = await self._client.post(f"/sessions/{session_id}/end")
        resp.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()
