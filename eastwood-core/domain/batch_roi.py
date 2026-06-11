"""
Batch ROI computation.

Answers the only question that matters for vendor spend:
  "Did this batch of leads make money, and how much per dollar in?"

Metrics are computed live from the leads + call_records + quote_records tables
so they update as outcomes are tagged — no separate ETL needed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BatchROI:
    batch_id: str
    vendor_name: str
    source_tag: str
    cost_cents: int
    lead_count: int
    contacted: int          # at least one answered call
    qualified: int          # quote was generated
    closed: int             # outcome = 'sold'
    total_ap_cents: int     # sum of ap_cents on closed leads
    avg_ap_cents: int
    contact_rate: float     # contacted / lead_count
    qualification_rate: float  # qualified / contacted
    quote_rate: float       # qualified / lead_count
    close_rate: float       # closed / qualified
    net_ap_cents: int       # total_ap_cents - cost_cents
    roi_multiplier: float   # total_ap / cost  (13.3x = great, <1x = trash)
    cost_per_lead_cents: int
    cost_per_policy_cents: int  # cost / closed  (0 if no closes)

    def is_profitable(self) -> bool:
        return self.roi_multiplier >= 3.0

    def verdict(self) -> str:
        if self.roi_multiplier >= 10.0:
            return "scale_immediately"
        if self.roi_multiplier >= 5.0:
            return "strong"
        if self.roi_multiplier >= 3.0:
            return "profitable"
        if self.roi_multiplier >= 1.0:
            return "marginal"
        return "kill_vendor"


def _safe_rate(num: int, denom: int) -> float:
    return round(num / denom, 4) if denom > 0 else 0.0


def compute_batch_roi(
    batch_id: str,
    vendor_name: str,
    source_tag: str,
    cost_cents: int,
    lead_count: int,
    contacted: int,
    qualified: int,
    closed: int,
    total_ap_cents: int,
) -> BatchROI:
    avg_ap = (total_ap_cents // closed) if closed > 0 else 0

    return BatchROI(
        batch_id=batch_id,
        vendor_name=vendor_name,
        source_tag=source_tag,
        cost_cents=cost_cents,
        lead_count=lead_count,
        contacted=contacted,
        qualified=qualified,
        closed=closed,
        total_ap_cents=total_ap_cents,
        avg_ap_cents=avg_ap,
        contact_rate=_safe_rate(contacted, lead_count),
        qualification_rate=_safe_rate(qualified, contacted),
        quote_rate=_safe_rate(qualified, lead_count),
        close_rate=_safe_rate(closed, qualified),
        net_ap_cents=total_ap_cents - cost_cents,
        roi_multiplier=round(total_ap_cents / cost_cents, 2) if cost_cents > 0 else 0.0,
        cost_per_lead_cents=(cost_cents // lead_count) if lead_count > 0 else 0,
        cost_per_policy_cents=(cost_cents // closed) if closed > 0 else 0,
    )
