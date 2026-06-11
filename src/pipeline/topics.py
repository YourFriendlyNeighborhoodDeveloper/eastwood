from dataclasses import dataclass
from typing import Dict

@dataclass(frozen=True)
class Topics:
    leads_raw: str
    leads_normalized: str
    leads_validated: str
    leads_enriched: str
    leads_scored: str
    leads_routed: str
    consent_events: str
    touches_events: str
    suppression_events: str
    dlq_events: str

    @staticmethod
    def from_dict(d: Dict[str, str]) -> "Topics":
        return Topics(
            leads_raw=d["leads_raw"],
            leads_normalized=d["leads_normalized"],
            leads_validated=d["leads_validated"],
            leads_enriched=d["leads_enriched"],
            leads_scored=d["leads_scored"],
            leads_routed=d["leads_routed"],
            consent_events=d["consent_events"],
            touches_events=d["touches_events"],
            suppression_events=d["suppression_events"],
            dlq_events=d["dlq_events"],
        )