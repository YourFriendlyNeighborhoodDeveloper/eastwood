from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class QuoteRequest:
    lead_id: str
    product_line: str
    age: int | None
    state: str | None
    zip_code: str | None
    county: str | None
    face_value: int  # cents
    tobacco: bool
    gender: str | None
    condition_codes: set[str]
    rate_tier: str | None = None
    part_a_date: str | None = None
    part_b_date: str | None = None


@dataclass
class CarrierQuote:
    carrier: str
    product_line: str
    monthly_premium: float
    face_value: int | None
    rate_tier: str
    plan_name: str | None = None
    plan_id: str | None = None
    source: str = "formula"  # "cms_api" | "rate_table" | "formula"
    raw_data: dict | None = None


@dataclass
class QuoteResponse:
    quotes: list[CarrierQuote]
    source: str
    fetched_at: str
    errors: list[str] = field(default_factory=list)

    @property
    def best(self) -> CarrierQuote | None:
        return self.quotes[0] if self.quotes else None


class CarrierQuoteAdapter(ABC):
    name: str = "unknown"
    supported_products: list[str] = []

    @abstractmethod
    async def get_quotes(self, request: QuoteRequest) -> list[CarrierQuote]: ...

    def can_handle(self, product_line: str, state: str | None) -> bool:
        return product_line in self.supported_products
