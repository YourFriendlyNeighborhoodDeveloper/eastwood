"""Rate-table provenance: only rows from a real rate sheet are carrier rates.

scripts/seed_rate_tables.py generates formula output under real carrier names.
It used to be seeded at boot, which turned every quote into a "carrier" quote
naming a carrier (e.g. "Prosperity Life, $75.88/mo") — a carrier rate that no
carrier ever published. Those rows are now tagged synthetic and never quoted.
"""
from __future__ import annotations

from domain.quoting.adapters.rate_table import SYNTHETIC_SOURCE, RateTableAdapter
from domain.quoting.base import QuoteRequest
from scripts.seed_rate_tables import _all_seed_rows


class _CapturingSession:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)

        class _R:
            def scalars(self):
                return []
        return _R()


def test_every_generated_sample_row_is_tagged_synthetic():
    rows = _all_seed_rows()
    assert rows and all(r["source"] == SYNTHETIC_SOURCE for r in rows)


async def test_adapter_only_reads_rows_with_real_provenance():
    session = _CapturingSession()
    req = QuoteRequest(lead_id="x", product_line="final_expense", age=67, state="MI",
                       face_value=1_500_000, tobacco=False, zip_code=None, county=None,
                       gender=None, condition_codes=set())
    assert await RateTableAdapter(session).get_quotes(req) == []

    sql = str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))
    assert "carrier_rate_tables.source IS NOT NULL" in sql
    assert f"carrier_rate_tables.source != '{SYNTHETIC_SOURCE}'" in sql


def test_boot_no_longer_seeds_sample_rates():
    import inspect

    import app.main as main

    src = inspect.getsource(main.lifespan)
    assert "_all_seed_rows" not in src
