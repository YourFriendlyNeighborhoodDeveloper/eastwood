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


# How each rate source is described to anything outside eastwood-core.
#
# FormulaAdapter output is a curve fitted to mid-market benchmarks. No carrier
# has quoted it, no carrier is obliged to honour it, and no carrier has even
# been selected. It must never leave core described as a carrier rate.
_SOURCE_LABELS: dict[str, dict] = {
    "formula": {
        "quote_type":   "estimated",
        "rate_source":  "formula_adapter",
        "carrier_rate": False,
    },
    "rate_table": {
        "quote_type":   "carrier",
        "rate_source":  "rate_table",
        "carrier_rate": True,
    },
    "cms_api": {
        "quote_type":   "carrier",
        "rate_source":  "cms_api",
        "carrier_rate": True,
    },
}

# An unrecognised source is treated as an estimate. Failing safe here means a
# new adapter cannot accidentally present itself as a bindable carrier rate.
_UNKNOWN_LABELS: dict = {
    "quote_type":   "estimated",
    "rate_source":  "unknown",
    "carrier_rate": False,
}


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

    @property
    def _labels(self) -> dict:
        return _SOURCE_LABELS.get(self.source, _UNKNOWN_LABELS)

    @property
    def quote_type(self) -> str:
        """"estimated" or "carrier"."""
        return self._labels["quote_type"]

    @property
    def rate_source(self) -> str:
        return self._labels["rate_source"]

    @property
    def carrier_rate(self) -> bool:
        """True only when the premium came from a carrier-published source."""
        return self._labels["carrier_rate"]

    @property
    def bindable(self) -> bool:
        """Whether this premium can be bound as written.

        Always False. Eastwood submits no application and receives no
        underwriting decision, so no quote it produces is bindable — not even
        one read from a real carrier rate sheet, whose tier still depends on
        underwriting. Flip this only when a carrier binding integration exists.
        """
        return False

    def labels(self) -> dict:
        """The honesty fields that accompany this quote wherever it goes."""
        return {
            "quote_type":   self.quote_type,
            "rate_source":  self.rate_source,
            "carrier_rate": self.carrier_rate,
            "bindable":     self.bindable,
        }


@dataclass
class QuoteResponse:
    quotes: list[CarrierQuote]
    source: str
    fetched_at: str
    errors: list[str] = field(default_factory=list)

    @property
    def best(self) -> CarrierQuote | None:
        return self.quotes[0] if self.quotes else None

    def labels(self) -> dict:
        """Labels for the quote a caller would act on. No quotes -> assume estimate."""
        best = self.best
        if best is None:
            return {**_UNKNOWN_LABELS, "bindable": False}
        return best.labels()


class CarrierQuoteAdapter(ABC):
    name: str = "unknown"
    supported_products: list[str] = []

    @abstractmethod
    async def get_quotes(self, request: QuoteRequest) -> list[CarrierQuote]: ...

    def can_handle(self, product_line: str, state: str | None) -> bool:
        return product_line in self.supported_products
