"""
National Do Not Call Registry clearance.

Distinct from the `suppressions` table, which records prospects who asked *us*
directly to stop calling (entity-specific DNC). This module is about the federal
registry: whether the number may be cold-called at all.

The gate fails closed. A lead is dialable only when its clearance is an explicit
`clear` or `exempt`. `unknown` — the default for every newly imported lead — is
not dialable.
"""
from __future__ import annotations

import csv
import logging
import pathlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Clearance states ──────────────────────────────────────────────────────────

UNKNOWN = "unknown"   # never checked, or the provider could not decide
CLEAR = "clear"       # a provider attested the number is not on the registry
BLOCKED = "blocked"   # the number is on the registry
EXEMPT = "exempt"     # operator-asserted exemption (EBR, express written consent)

VALID_STATUSES = frozenset({UNKNOWN, CLEAR, BLOCKED, EXEMPT})

#: The only two states from which an outbound dial may be placed.
DIALABLE_STATUSES = frozenset({CLEAR, EXEMPT})

#: States a provider is allowed to return. `exempt` is an operator decision,
#: never a scrub result.
SCRUBBABLE_STATUSES = frozenset({UNKNOWN, CLEAR, BLOCKED})


def is_dialable(status: str | None) -> bool:
    return (status or UNKNOWN) in DIALABLE_STATUSES


# ── Provider interface ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScrubResult:
    """One clearance decision, with enough provenance to defend it later."""

    status: str                 # clear | blocked | unknown
    provider: str               # which provider decided
    checked_at: datetime        # when
    source: str | None = None   # compact provenance string, when exposed
    # Structured audit trail, persisted to leads.national_dnc_audit. Must never
    # contain a raw SAN, credentials, or any registry phone number.
    audit: dict | None = None

    def __post_init__(self) -> None:
        if self.status not in SCRUBBABLE_STATUSES:
            raise ValueError(f"a scrub provider may not return {self.status!r}")


class DncScrubProvider(ABC):
    """Checks a number against the National DNC Registry.

    Implementations must return UNKNOWN rather than guessing. UNKNOWN is not
    dialable, so a provider that fails, times out, or has no opinion cannot
    cause a call.
    """

    name: str = "unknown"

    @abstractmethod
    async def scrub(self, phone_e164: str) -> ScrubResult:
        """Return the registry status of a single E.164 number."""


# ── Local CSV provider ────────────────────────────────────────────────────────

class LocalCsvScrubProvider(DncScrubProvider):
    """Blocklist-only provider backed by a local, gitignored CSV of E.164 numbers.

    A hit is proof the number is on the registry. A miss proves nothing: this
    file is a blocklist, not a copy of the registry, so absence from it cannot
    attest to clearance. A miss therefore returns UNKNOWN, never CLEAR.

    Only a provider that actually queries the registry may return CLEAR. That
    is why this one, alone, can never make a lead dialable.

    A missing or unreadable file yields UNKNOWN for every number: no file means
    no knowledge, and no knowledge means no dial.
    """

    name = "local_csv"

    def __init__(self, path: str | pathlib.Path) -> None:
        self._path = pathlib.Path(path)
        self._blocked: frozenset[str] | None = None
        self._source: str | None = None

    @classmethod
    def from_settings(cls, settings) -> "LocalCsvScrubProvider":
        return cls(settings.national_dnc_blocklist_path)

    def _load(self) -> frozenset[str]:
        if self._blocked is not None:
            return self._blocked

        if not self._path.exists():
            logger.warning(
                "national DNC blocklist %s not found — every number scrubs as %s "
                "and no lead will be dialable",
                self._path, UNKNOWN,
            )
            self._blocked = frozenset()
            self._source = None
            return self._blocked

        numbers: set[str] = set()
        try:
            with self._path.open(newline="", encoding="utf-8-sig") as handle:
                for row in csv.reader(handle):
                    if not row:
                        continue
                    raw = (row[0] or "").strip()
                    if not raw or raw.startswith("#"):
                        continue
                    normalized = _to_e164(raw)
                    if normalized:
                        numbers.add(normalized)
        except OSError as exc:
            logger.error("could not read national DNC blocklist %s: %s", self._path, exc)
            self._blocked = frozenset()
            self._source = None
            return self._blocked

        mtime = int(self._path.stat().st_mtime)
        self._blocked = frozenset(numbers)
        self._source = f"{self._path.name}@{mtime} ({len(numbers)} entries)"[:128]
        logger.info("loaded %d national DNC blocklist entries from %s", len(numbers), self._path)
        return self._blocked

    def reload(self) -> None:
        """Drop the cached blocklist so the next scrub re-reads the file."""
        self._blocked = None
        self._source = None

    async def scrub(self, phone_e164: str) -> ScrubResult:
        blocked = self._load()
        now = datetime.now(timezone.utc)
        status = BLOCKED if phone_e164 in blocked else UNKNOWN
        return ScrubResult(status=status, provider=self.name, checked_at=now, source=self._source)


def _to_e164(raw: str) -> str | None:
    """Tolerate the formats a hand-maintained blocklist arrives in."""
    from domain.normalization import normalize_phone

    return normalize_phone(raw)


# ── Provider registry ─────────────────────────────────────────────────────────
#
# Keyed by name so no vendor is hardcoded into the dial path. Each provider
# builds itself from settings via from_settings(). Set DNC_SCRUB_PROVIDER to a
# registered name. ftc_registry is imported lazily to avoid a circular import
# (it depends on the base classes defined above).


def _provider_registry() -> dict[str, type[DncScrubProvider]]:
    from .ftc_registry import FtcRegistryScrubProvider

    return {
        LocalCsvScrubProvider.name: LocalCsvScrubProvider,
        FtcRegistryScrubProvider.name: FtcRegistryScrubProvider,
    }


def get_scrub_provider(settings) -> DncScrubProvider:
    name = settings.dnc_scrub_provider
    registry = _provider_registry()
    factory = registry.get(name)
    if factory is None:
        raise ValueError(
            f"unknown DNC scrub provider {name!r}; registered: {sorted(registry)}"
        )
    return factory.from_settings(settings)
