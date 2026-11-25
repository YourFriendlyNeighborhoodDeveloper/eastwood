"""Carrier rate sheets: import as data, verify against the carrier, quote exactly.

A rate sheet is a carrier's published rate book for one plan: rates by age,
sex, tobacco class and underwriting tier, the plan's face-amount limits, the
states it is sold in, its effective dates, its annual policy fee and its
payment-mode (modal) factors. Premiums are computed exactly as a carrier rate
book specifies:

  per_1000_annual   annual = rate x face / 1000 + annual policy fee
                    modal  = annual x modal factor[mode]   (monthly factor e.g. 0.0875)
  per_1000_monthly  monthly = rate x face / 1000 + monthly policy fee
                    modal   = monthly x modal factor[mode]  (monthly factor 1)
  flat_monthly      the sheet lists the monthly premium for each face amount
  flat_annual       the sheet lists the annual premium for each face amount;
                    modal = annual x modal factor[mode]

A "flat" (grid) sheet quotes only the face amounts printed in it; a per-$1,000
sheet quotes any face between its limits in its increment. Every quote is for
a payment mode the sheet defines a factor for (SMS quotes are monthly).

with Decimal arithmetic and the sheet's rounding convention ("final": round the
modal premium once; "annual_then_modal": round the annual premium, then the
modal premium).

Trust is earned, not assumed. A sheet imports as DRAFT and quotes nobody until
verify_rate_sheet() reproduces spot checks taken from the carrier's own quote
tool or illustration — to the cent. A sheet that fails any check stays a draft.
Retired, expired, not-yet-effective, out-of-state and (in production) test
sheets never price a quote. Nothing here estimates, interpolates or invents a
rate: a missing cell means no quote, with the reason.
"""
from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.models import RateSheet, RateSheetRate
from domain.tcpa import supported_states

RATE_UNITS = ("per_1000_annual", "per_1000_monthly", "flat_monthly", "flat_annual")
GRID_UNITS = ("flat_monthly", "flat_annual")          # premiums listed per face amount
MONTHLY_BASED = ("per_1000_monthly", "flat_monthly")  # figures are already monthly
ROUNDING = ("final", "annual_then_modal")
CENT = Decimal("0.01")
_TRUE = {"y", "yes", "true", "t", "1", "tobacco", "smoker", "s", "tob"}
_FALSE = {"n", "no", "false", "f", "0", "non-tobacco", "nontobacco", "non-smoker", "nonsmoker", "ns", "nt"}


class RateSheetError(ValueError):
    pass


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_tobacco(raw) -> bool:
    v = str(raw).strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise RateSheetError(f"tobacco must be yes/no, got {raw!r}")


def parse_sex(raw) -> str:
    v = str(raw).strip().upper()[:1]
    if v in ("M", "F", "U"):
        return v
    raise RateSheetError(f"sex must be M, F or U (unisex), got {raw!r}")


def compute_premium(*, rate_unit: str, rate: Decimal, face: int, policy_fee_annual: Decimal,
                    modal_factor: Decimal, rounding: str = "final",
                    policy_fee_monthly: Decimal = Decimal(0)) -> Decimal:
    if rate_unit == "per_1000_annual":
        annual = rate * Decimal(face) / Decimal(1000) + policy_fee_annual
        if rounding == "annual_then_modal":
            annual = money(annual)
        return money(annual * modal_factor)
    if rate_unit == "per_1000_monthly":
        monthly = rate * Decimal(face) / Decimal(1000) + policy_fee_monthly
        if rounding == "annual_then_modal":      # i.e. round the base (monthly) premium first
            monthly = money(monthly)
        return money(monthly * modal_factor)
    if rate_unit in GRID_UNITS:
        return money(rate * modal_factor)
    raise RateSheetError(f"unknown rate unit {rate_unit!r}")


def _meta_rounding(sheet) -> str:
    factors = sheet.modal_factors or {}
    return str(factors.get("_rounding", "final"))


# ── Import ───────────────────────────────────────────────────────────────────

REQUIRED_META = ("carrier", "product_line", "plan_name", "states", "effective_date", "rate_unit",
                 "modal_factors", "min_face", "max_face", "source_document")


def _validate_meta(meta: dict) -> dict:
    missing = [k for k in REQUIRED_META if meta.get(k) in (None, "", [])]
    if missing:
        raise RateSheetError(f"rate sheet metadata missing: {', '.join(missing)}")
    if meta["rate_unit"] not in RATE_UNITS:
        raise RateSheetError(f"rate_unit must be one of {RATE_UNITS}")
    states = [str(s).strip().upper() for s in meta["states"]]
    bad = sorted(set(states) - supported_states())
    if bad:
        raise RateSheetError(f"unsupported states: {', '.join(bad)}")
    factors = {str(k): v for k, v in dict(meta["modal_factors"]).items()}
    rounding = str(meta.get("rounding", "final"))
    if rounding not in ROUNDING:
        raise RateSheetError(f"rounding must be one of {ROUNDING}")
    for mode, factor in factors.items():
        try:
            if Decimal(str(factor)) <= 0:
                raise RateSheetError(f"modal factor for {mode} must be positive")
        except InvalidOperation:
            raise RateSheetError(f"modal factor for {mode} is not a number") from None
    if "monthly" not in factors:
        raise RateSheetError("modal_factors must include 'monthly' (the SMS quote mode)")
    if meta["rate_unit"] in MONTHLY_BASED and Decimal(str(factors["monthly"])) != 1:
        raise RateSheetError(f"{meta['rate_unit']} figures are monthly already: the monthly modal factor must be 1")
    fee_a = Decimal(str(meta.get("policy_fee_annual") or 0))
    fee_m = Decimal(str(meta.get("policy_fee_monthly") or 0))
    if fee_a < 0 or fee_m < 0:
        raise RateSheetError("policy fees cannot be negative")
    if meta["rate_unit"] in GRID_UNITS and (fee_a or fee_m):
        raise RateSheetError("a premium grid already includes the policy fee: set policy fees to 0")
    if meta["rate_unit"] == "per_1000_annual" and fee_m:
        raise RateSheetError("per_1000_annual sheets take an annual policy fee (policy_fee_annual)")
    if meta["rate_unit"] == "per_1000_monthly" and fee_a:
        raise RateSheetError("per_1000_monthly sheets take a monthly policy fee (policy_fee_monthly)")
    eff = date.fromisoformat(str(meta["effective_date"]))
    exp = date.fromisoformat(str(meta["expires_date"])) if meta.get("expires_date") else None
    if exp and exp <= eff:
        raise RateSheetError("expires_date must be after effective_date")
    min_face, max_face = int(meta["min_face"]), int(meta["max_face"])
    inc = int(meta.get("face_increment", 1000))
    if not (0 < min_face <= max_face) or inc <= 0:
        raise RateSheetError("face limits must satisfy 0 < min_face <= max_face and face_increment > 0")
    questions = meta.get("tier_questions") or None
    if questions:
        for q in questions:
            if not {"key", "text", "yes_tier"} <= set(q):
                raise RateSheetError("each tier question needs key, text and yes_tier")
    return {
        "carrier": str(meta["carrier"])[:128], "product_line": str(meta["product_line"]),
        "plan_name": str(meta["plan_name"])[:128], "states": states, "effective_date": eff,
        "expires_date": exp, "rate_unit": meta["rate_unit"],
        "policy_fee_annual": fee_a, "policy_fee_monthly": fee_m,
        "modal_factors": {**factors, "_rounding": rounding}, "min_face": min_face, "max_face": max_face,
        "face_increment": inc, "default_tier": str(meta.get("default_tier", "level")).lower(),
        "tier_questions": questions, "source_document": str(meta["source_document"]),
        "is_test": bool(meta.get("is_test", False)),
    }


def read_rate_rows(csv_path: Path, rate_unit: str) -> list[dict]:
    rows, seen = [], set()
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for n, r in enumerate(csv.DictReader(f), start=2):
            try:
                age = int(str(r["age"]).strip())
                sex = parse_sex(r.get("sex", "U"))
                tob = parse_tobacco(r["tobacco"])
                tier = str(r.get("tier") or "level").strip().lower()
                rate = Decimal(str(r["rate"]).replace("$", "").replace(",", "").strip())
                face = int(str(r.get("face") or 0).replace(",", "").strip() or 0)
            except (KeyError, ValueError, InvalidOperation, RateSheetError) as exc:
                raise RateSheetError(f"row {n}: {exc}") from None
            if not 0 < age < 120 or rate <= 0:
                raise RateSheetError(f"row {n}: age must be 1-119 and rate positive")
            if rate_unit in GRID_UNITS and face <= 0:
                raise RateSheetError(f"row {n}: {rate_unit} sheets need a face column")
            if rate_unit not in GRID_UNITS and face:
                raise RateSheetError(f"row {n}: per-$1,000 rates take no face column")
            key = (age, sex, tob, tier, face)
            if key in seen:
                raise RateSheetError(f"row {n}: duplicate cell {key}")
            seen.add(key)
            rows.append({"age": age, "sex": sex, "tobacco": tob, "tier": tier, "face": face, "rate": rate})
    if not rows:
        raise RateSheetError("rate sheet has no rows")
    return rows


async def import_rate_sheet(session, csv_path: Path | str, meta: dict, tenant_id: str) -> RateSheet:
    csv_path = Path(csv_path)
    m = _validate_meta(meta)
    rows = read_rate_rows(csv_path, m["rate_unit"])
    digest = hashlib.sha256(csv_path.read_bytes() + json.dumps(meta, sort_keys=True, default=str).encode()).hexdigest()
    sheet = RateSheet(tenant_id=tenant_id, file_sha256=digest, status="draft", **m)
    session.add(sheet)
    await session.flush()
    for i in range(0, len(rows), 2000):
        await session.execute(pg_insert(RateSheetRate).values(
            [{"sheet_id": sheet.id, **r} for r in rows[i:i + 2000]]))
    return sheet


# ── Quote ────────────────────────────────────────────────────────────────────

@dataclass
class SheetQuote:
    sheet_id: str
    carrier: str
    plan_name: str
    product_line: str
    effective_date: str
    expires_date: str | None
    source_document: str
    is_test: bool
    tier: str
    face: int
    mode: str
    premium: Decimal
    rate: Decimal
    policy_fee_annual: Decimal
    modal_factor: Decimal
    rounding: str
    inputs: dict = field(default_factory=dict)

    def provenance(self) -> dict:
        return {
            "rate_sheet_id": self.sheet_id, "carrier": self.carrier, "plan_name": self.plan_name,
            "product_line": self.product_line, "effective_date": self.effective_date,
            "expires_date": self.expires_date, "source_document": self.source_document,
            "is_test_sheet": self.is_test, "tier": self.tier, "face": self.face, "mode": self.mode,
            "premium": str(self.premium), "rate": str(self.rate),
            "policy_fee_annual": str(self.policy_fee_annual), "modal_factor": str(self.modal_factor),
            "rounding": self.rounding, "inputs": self.inputs,
        }


async def grid_faces(session, sheet_id) -> list[int]:
    """The face amounts a premium-grid sheet actually lists."""
    return sorted(f for (f,) in (await session.execute(
        select(RateSheetRate.face).where(RateSheetRate.sheet_id == sheet_id).distinct())).all() if f)


async def available_faces(session, *, tenant_id: str, product_line: str, state: str | None,
                          today: date | None = None, allow_test: bool = False) -> list[int]:
    """Every face amount some verified, effective sheet sold in `state` can quote:
    the printed faces of grid sheets and min..max by increment of per-$1,000
    sheets. Prospect options are chosen from these, never between them."""
    today = today or datetime.now(timezone.utc).date()
    faces: set[int] = set()
    for s in await _candidate_sheets(session, tenant_id=tenant_id, product_line=product_line, today=today,
                                     allow_test=allow_test):
        if state not in (s.states or []):
            continue
        if s.rate_unit in GRID_UNITS:
            faces.update(await grid_faces(session, s.id))
        else:
            faces.update(range(s.min_face, s.max_face + 1, s.face_increment))
    return sorted(faces)


async def _candidate_sheets(session, *, tenant_id: str, product_line: str, today: date, allow_test: bool):
    q = select(RateSheet).where(
        RateSheet.tenant_id == tenant_id, RateSheet.product_line == product_line,
        RateSheet.status == "verified", RateSheet.effective_date <= today,
    )
    sheets = [s for s in (await session.execute(q)).scalars()
              if (s.expires_date is None or s.expires_date > today) and (allow_test or not s.is_test)]
    return sheets


async def quote_from_sheets(session, *, tenant_id: str, product_line: str, state: str | None, age: int | None,
                            sex: str | None, tobacco: bool | None, face: int, tier: str | None = None,
                            mode: str = "monthly", today: date | None = None,
                            allow_test: bool = False) -> tuple[list[SheetQuote], list[str]]:
    """Exact premiums from every verified, effective sheet that covers the case,
    cheapest first — and, when there are none, why."""
    today = today or datetime.now(timezone.utc).date()
    reasons: list[str] = []
    missing = [k for k, v in (("age", age), ("state", state), ("tobacco", tobacco)) if v is None]
    if missing:
        return [], [f"missing {', '.join(missing)}"]
    sheets = await _candidate_sheets(session, tenant_id=tenant_id, product_line=product_line,
                                     today=today, allow_test=allow_test)
    if not sheets:
        return [], [f"no verified, effective rate sheet for {product_line}"]
    out: list[SheetQuote] = []
    for s in sheets:
        label = f"{s.carrier} {s.plan_name}"
        if state not in (s.states or []):
            reasons.append(f"{label}: not sold in {state}")
            continue
        if s.rate_unit in GRID_UNITS:
            grid = await grid_faces(session, s.id)
            if face not in grid:
                shown = ", ".join(f"${f:,}" for f in grid[:8]) + (" ..." if len(grid) > 8 else "")
                reasons.append(f"{label}: face ${face:,} is not in the rate table ({shown})")
                continue
        elif not s.min_face <= face <= s.max_face or (face - s.min_face) % s.face_increment:
            reasons.append(f"{label}: face ${face:,} outside ${s.min_face:,}-${s.max_face:,} "
                           f"in ${s.face_increment:,} steps")
            continue
        use_tier = (tier or s.default_tier).lower()
        cell_face = face if s.rate_unit in GRID_UNITS else 0
        q = select(RateSheetRate).where(
            RateSheetRate.sheet_id == s.id, RateSheetRate.age == age, RateSheetRate.tobacco == tobacco,
            RateSheetRate.tier == use_tier, RateSheetRate.face == cell_face,
        )
        cells = {c.sex: c for c in (await session.execute(q)).scalars()}
        cell = cells.get("U") or (cells.get(sex) if sex else None)
        if cell is None:
            if cells and not sex:
                reasons.append(f"{label}: needs sex (rates differ by sex)")
            else:
                span = (await session.execute(select(func.min(RateSheetRate.age), func.max(RateSheetRate.age))
                                              .where(RateSheetRate.sheet_id == s.id))).one()
                reasons.append(f"{label}: no rate for age {age}, {'tobacco' if tobacco else 'non-tobacco'}, "
                               f"tier {use_tier} (sheet ages {span[0]}-{span[1]})")
            continue
        factors = s.modal_factors or {}
        if mode not in factors or mode.startswith("_"):
            reasons.append(f"{label}: no modal factor for {mode}")
            continue
        factor = Decimal(str(factors[mode]))
        premium = compute_premium(rate_unit=s.rate_unit, rate=Decimal(str(cell.rate)), face=face,
                                  policy_fee_annual=Decimal(str(s.policy_fee_annual)), modal_factor=factor,
                                  rounding=_meta_rounding(s),
                                  policy_fee_monthly=Decimal(str(getattr(s, "policy_fee_monthly", 0) or 0)))
        out.append(SheetQuote(
            sheet_id=str(s.id), carrier=s.carrier, plan_name=s.plan_name, product_line=s.product_line,
            effective_date=s.effective_date.isoformat(),
            expires_date=s.expires_date.isoformat() if s.expires_date else None,
            source_document=s.source_document, is_test=s.is_test, tier=use_tier, face=face, mode=mode,
            premium=premium, rate=Decimal(str(cell.rate)), policy_fee_annual=Decimal(str(s.policy_fee_annual)),
            modal_factor=factor, rounding=_meta_rounding(s),
            inputs={"age": age, "sex": cell.sex if cell.sex != "U" else (sex or "U"), "tobacco": tobacco,
                    "state": state, "face": face, "tier": use_tier, "mode": mode, "as_of": today.isoformat()},
        ))
    out.sort(key=lambda q: q.premium)
    return out, reasons


# ── Verify ───────────────────────────────────────────────────────────────────

MIN_CHECKS = 3


async def verify_rate_sheet(session, sheet_id, *, checks: list[dict], verified_by: str,
                            tenant_id: str) -> dict:
    """Mark a sheet verified only if every carrier-sourced spot check reproduces
    to the cent. checks: [{age, sex, tobacco, tier, face, mode, expected}]."""
    sheet = (await session.execute(select(RateSheet).where(
        RateSheet.id == sheet_id, RateSheet.tenant_id == tenant_id))).scalars().first()
    if sheet is None:
        raise RateSheetError("rate sheet not found")
    if not verified_by.strip():
        raise RateSheetError("verified_by is required")
    if len(checks) < MIN_CHECKS:
        raise RateSheetError(f"at least {MIN_CHECKS} spot checks from the carrier's own quote tool are required")
    # Every rate class the sheet holds must be checked at least once: an
    # extraction slip (a shifted column, a mislabelled table) shows up in the
    # class it affected, so a class nobody checked is a class nobody verified.
    present = (await session.execute(select(RateSheetRate.sex, RateSheetRate.tobacco, RateSheetRate.tier)
                                     .where(RateSheetRate.sheet_id == sheet.id).distinct())).all()
    covered = set()
    for c in checks:
        c_tier = str(c.get("tier") or sheet.default_tier).lower()
        c_tob = parse_tobacco(c["tobacco"])
        c_sex = parse_sex(c.get("sex", "U"))
        covered |= {(sx, c_tob, c_tier) for sx in (c_sex, "U")}
    uncovered = sorted(f"{sx} {'tobacco' if tb else 'non-tobacco'} {tr}" for sx, tb, tr in present
                       if (sx, tb, tr) not in covered)
    if uncovered:
        raise RateSheetError("spot checks must cover every rate class; add a check for: " + "; ".join(uncovered))
    results, ok = [], True
    for c in checks:
        tob = parse_tobacco(c["tobacco"])
        sex = parse_sex(c.get("sex", "U"))
        face = int(c["face"])
        mode = str(c.get("mode", "monthly"))
        expected = money(Decimal(str(c["expected"]).replace("$", "").replace(",", "")))
        tier = str(c.get("tier") or sheet.default_tier).lower()
        cell_face = face if sheet.rate_unit in GRID_UNITS else 0
        rows = {r.sex: r for r in (await session.execute(select(RateSheetRate).where(
            RateSheetRate.sheet_id == sheet.id, RateSheetRate.age == int(c["age"]),
            RateSheetRate.tobacco == tob, RateSheetRate.tier == tier, RateSheetRate.face == cell_face,
        ))).scalars()}
        cell = rows.get(sex) or rows.get("U")
        if cell is None or mode not in (sheet.modal_factors or {}) or mode.startswith("_"):
            got, match = None, False
        else:
            got = compute_premium(rate_unit=sheet.rate_unit, rate=Decimal(str(cell.rate)), face=face,
                                  policy_fee_annual=Decimal(str(sheet.policy_fee_annual)),
                                  modal_factor=Decimal(str(sheet.modal_factors[mode])),
                                  rounding=_meta_rounding(sheet),
                                  policy_fee_monthly=Decimal(str(getattr(sheet, "policy_fee_monthly", 0) or 0)))
            match = got == expected
        ok &= match
        results.append({"age": int(c["age"]), "sex": sex, "tobacco": tob, "tier": tier, "face": face,
                        "mode": mode, "expected": str(expected), "computed": str(got) if got is not None else None,
                        "match": match})
    log = {"verified_by": verified_by, "at": datetime.now(timezone.utc).isoformat(), "checks": results, "passed": ok}
    sheet.verification = (sheet.verification or []) + [log]
    if ok:
        sheet.status = "verified"
        sheet.verified_by = verified_by[:128]
        sheet.verified_at = datetime.now(timezone.utc)
    await session.flush()
    return {"sheet_id": str(sheet.id), "status": sheet.status, "passed": ok, "checks": results}


async def retire_rate_sheet(session, sheet_id, tenant_id: str) -> None:
    sheet = (await session.execute(select(RateSheet).where(
        RateSheet.id == sheet_id, RateSheet.tenant_id == tenant_id))).scalars().first()
    if sheet is None:
        raise RateSheetError("rate sheet not found")
    sheet.status = "retired"
    await session.flush()
