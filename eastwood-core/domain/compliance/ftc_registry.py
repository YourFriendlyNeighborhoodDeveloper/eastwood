"""
Official National Do Not Call Registry provider (US FTC).

The registry is not a per-number lookup service for bulk telemarketing. The
supported mechanism is to *download* the registered numbers for the area codes
your Subscription Account Number (SAN) is subscribed to, then check numbers
against that local dataset. This module does exactly that:

  - a downloader (network seam) fetches one area-code file at a time using the
    documented authenticated download interface;
  - refresh_registry() downloads every subscribed area code atomically, validates
    and checksums each file, and only then swaps the active dataset in;
  - FtcRegistryScrubProvider checks a normalized number against the active,
    validated dataset and fails closed.

Security invariants:
  - the raw SAN, username, and password never touch disk, logs, or audit records;
    only a SAN fingerprint (a truncated hash) is persisted;
  - registry phone numbers are never logged and never returned through an API;
  - a number can only be reported `clear` against a complete, in-scope, fresh,
    subscription-matched dataset. Everything else is `unknown`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .dnc_scrub import BLOCKED, CLEAR, UNKNOWN, DncScrubProvider, ScrubResult

logger = logging.getLogger(__name__)

VERSION = "ftc_registry/1"

_MANIFEST_NAME = "manifest.json"
_AREACODES_DIR = "areacodes"
_AREA_CODE_RE = re.compile(r"^[2-9]\d{2}$")


# ── Secret handling ───────────────────────────────────────────────────────────

def san_fingerprint(san: str | None) -> str:
    """A non-reversible tag for a SAN. Persisted and logged in place of the SAN.

    Empty when no SAN is configured, which no real dataset can match — so a
    provider with no SAN can never report `clear`.
    """
    if not san:
        return ""
    return "sha256:" + hashlib.sha256(san.encode("utf-8")).hexdigest()[:16]


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ── Number handling ───────────────────────────────────────────────────────────

def split_us_number(phone_e164: str) -> tuple[str, str] | None:
    """Normalize to (area_code, 10-digit national number), or None if unusable."""
    import phonenumbers

    try:
        parsed = phonenumbers.parse(phone_e164, "US")
    except Exception:
        return None
    if parsed.country_code != 1 or not phonenumbers.is_valid_number(parsed):
        return None
    national = str(parsed.national_number)
    if len(national) != 10:
        return None
    return national[:3], national


def _parse_flat_file(raw: bytes, area_code: str) -> list[str]:
    """Parse a registry flat file into sorted 10-digit numbers for one area code.

    Raises ValueError on anything that is not a well-formed, non-empty file of
    numbers for this area code — an HTML error page, a truncated download, or a
    file for the wrong area code all fail here rather than being trusted.
    """
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"area {area_code}: not valid UTF-8") from exc

    numbers: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digits = re.sub(r"\D", "", line)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ValueError(f"area {area_code}: unparseable line {lineno}")
        if not digits.startswith(area_code):
            raise ValueError(f"area {area_code}: line {lineno} is a different area code")
        numbers.add(digits)

    if not numbers:
        raise ValueError(f"area {area_code}: file contained no numbers (partial download?)")
    return sorted(numbers)


def _serialize_numbers(numbers: list[str]) -> bytes:
    return ("\n".join(numbers) + "\n").encode("utf-8")


# ── Dataset manifest ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DatasetManifest:
    version: str
    downloaded_at: datetime
    san_fingerprint: str
    area_codes: tuple[str, ...]
    complete: bool
    files: dict[str, dict]  # area_code -> {"checksum": str, "count": int}

    def age_days(self, now: datetime) -> int:
        return max(0, (now - self.downloaded_at).days)

    @classmethod
    def load(cls, data_dir: Path) -> "DatasetManifest | None":
        path = data_dir / _MANIFEST_NAME
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text("utf-8"))
            return cls(
                version=str(payload["version"]),
                downloaded_at=datetime.fromisoformat(payload["downloaded_at"]),
                san_fingerprint=str(payload.get("san_fingerprint", "")),
                area_codes=tuple(payload.get("area_codes", [])),
                complete=bool(payload["complete"]),
                files=dict(payload.get("files", {})),
            )
        except (ValueError, KeyError, TypeError) as exc:
            logger.error("national DNC manifest at %s is invalid: %s", path, exc)
            return None


# ── Provider ──────────────────────────────────────────────────────────────────

class FtcRegistryScrubProvider(DncScrubProvider):
    """Checks numbers against a downloaded, validated National Registry dataset."""

    name = "ftc_registry"

    def __init__(
        self,
        data_directory: str | Path,
        subscribed_area_codes: frozenset[str],
        max_age_days: int,
        configured_san_fingerprint: str,
    ) -> None:
        self._dir = Path(data_directory)
        self._subscribed = subscribed_area_codes
        self._max_age_days = max_age_days
        self._san_fp = configured_san_fingerprint
        self._file_cache: dict[tuple[str, str], frozenset[str] | None] = {}

    @classmethod
    def from_settings(cls, settings) -> "FtcRegistryScrubProvider":
        return cls(
            data_directory=settings.dnc_data_directory,
            subscribed_area_codes=parse_area_codes(settings.dnc_area_codes),
            max_age_days=settings.dnc_max_data_age_days,
            configured_san_fingerprint=san_fingerprint(settings.dnc_san),
        )

    async def scrub(self, phone_e164: str) -> ScrubResult:
        now = datetime.now(timezone.utc)
        split = split_us_number(phone_e164)
        if split is None:
            return self._result(UNKNOWN, now, reason="unevaluable_number")
        area_code, national = split

        manifest = DatasetManifest.load(self._dir)
        if manifest is None:
            return self._result(UNKNOWN, now, area_code=area_code, reason="no_dataset")

        entry = manifest.files.get(area_code)
        if entry is None:
            # No downloaded, validated file for this area code.
            return self._result(UNKNOWN, now, manifest=manifest, area_code=area_code,
                                 reason="out_of_scope")

        numbers = self._load_area_file(area_code, str(entry.get("checksum", "")))
        if numbers is None:
            return self._result(UNKNOWN, now, manifest=manifest, area_code=area_code,
                                 reason="corrupt_dataset")

        checksum = str(entry.get("checksum", ""))
        if national in numbers:
            # A hit is trustworthy even from a stale list: registration only
            # under-reports over time, it does not invent listings.
            return self._result(BLOCKED, now, manifest=manifest, area_code=area_code,
                                 checksum=checksum, reason="listed")

        # Absent — clear only against a complete, fresh, subscription-matched dataset.
        if not manifest.complete:
            return self._result(UNKNOWN, now, manifest=manifest, area_code=area_code,
                                 checksum=checksum, reason="incomplete_dataset")
        age = manifest.age_days(now)
        if age > self._max_age_days:
            return self._result(UNKNOWN, now, manifest=manifest, area_code=area_code,
                                 checksum=checksum, reason="stale_dataset")
        if not self._san_fp or manifest.san_fingerprint != self._san_fp:
            return self._result(UNKNOWN, now, manifest=manifest, area_code=area_code,
                                 checksum=checksum, reason="subscription_unverified")

        return self._result(CLEAR, now, manifest=manifest, area_code=area_code,
                            checksum=checksum, reason="absent_from_complete_dataset")

    # ── internals ─────────────────────────────────────────────────────────────

    def _load_area_file(self, area_code: str, expected_checksum: str) -> frozenset[str] | None:
        cache_key = (area_code, expected_checksum)
        if cache_key in self._file_cache:
            return self._file_cache[cache_key]

        path = self._dir / _AREACODES_DIR / f"{area_code}.txt"
        result: frozenset[str] | None
        if not path.exists():
            result = None
        else:
            data = path.read_bytes()
            if _sha256(data) != expected_checksum:
                logger.error("national DNC file %s failed checksum verification", path)
                result = None
            else:
                try:
                    result = frozenset(_parse_flat_file(data, area_code))
                except ValueError as exc:
                    logger.error("national DNC file %s is corrupt: %s", path, exc)
                    result = None

        self._file_cache[cache_key] = result
        return result

    def _result(
        self,
        status: str,
        now: datetime,
        *,
        manifest: DatasetManifest | None = None,
        area_code: str | None = None,
        checksum: str | None = None,
        reason: str = "",
    ) -> ScrubResult:
        downloaded_at = manifest.downloaded_at.isoformat() if manifest else None
        age_days = manifest.age_days(now) if manifest else None
        scope = list(manifest.area_codes) if manifest else sorted(self._subscribed)
        dataset_fp = manifest.san_fingerprint if manifest else None

        audit = {
            "provider": self.name,
            "version": VERSION,
            "checked_at": now.isoformat(),
            "reason": reason,
            "dataset_downloaded_at": downloaded_at,
            "dataset_age_days": age_days,
            "san_fingerprint": dataset_fp,          # never the raw SAN
            "area_code_scope": scope,
            "area_code": area_code,
            "checksum": checksum,
        }
        source = self._compact_source(status, downloaded_at, age_days, area_code, checksum)
        return ScrubResult(status=status, provider=self.name, checked_at=now,
                           source=source, audit=audit)

    @staticmethod
    def _compact_source(status, downloaded_at, age_days, area_code, checksum) -> str:
        parts = [VERSION, status]
        if downloaded_at:
            parts.append(f"dl={downloaded_at[:10]}")
        if age_days is not None:
            parts.append(f"age={age_days}d")
        if area_code:
            parts.append(f"ac={area_code}")
        if checksum:
            parts.append(f"ck={checksum.split(':')[-1][:8]}")
        return " ".join(parts)[:128]


def parse_area_codes(raw: str | None) -> frozenset[str]:
    if not raw:
        return frozenset()
    return frozenset(
        code for code in (c.strip() for c in raw.split(",")) if _AREA_CODE_RE.match(code)
    )


# ── Download seam ─────────────────────────────────────────────────────────────

class RegistryDownloader:
    """Fetches one area-code file from the registry. Injected so refresh can run
    without a live registry (tests supply a double for the network only)."""

    async def download_area_code(self, area_code: str) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError


class FtcRegistryDownloader(RegistryDownloader):
    """Downloads one area-code flat file via the FTC's authenticated automated
    download interface.

    The endpoint URL and form-field layout follow the documented interface and
    are operator-configured (DNC_REGISTRY_DOWNLOAD_URL) — this code does not
    hardcode or scrape the website. Credentials are sent in the request body and
    are never logged.
    """

    def __init__(self, url: str, san: str, username: str, password: str, timeout: float = 120.0) -> None:
        if not url:
            raise ValueError("DNC_REGISTRY_DOWNLOAD_URL is not configured")
        if not san:
            raise ValueError("DNC_SAN is not configured")
        self._url = url
        self._san = san
        self._username = username
        self._password = password
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings) -> "FtcRegistryDownloader":
        return cls(
            url=settings.dnc_registry_download_url,
            san=settings.dnc_san,
            username=settings.dnc_registry_username,
            password=settings.dnc_registry_password,
        )

    async def download_area_code(self, area_code: str) -> bytes:
        import httpx

        # Credentials travel in the body; nothing here is logged.
        form = {
            "san": self._san,
            "username": self._username,
            "password": self._password,
            "areaCode": area_code,
            "fileType": "flat",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(self._url, data=form)
            resp.raise_for_status()
            return resp.content


# ── Atomic refresh ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RefreshResult:
    ok: bool
    area_codes: tuple[str, ...]
    total_numbers: int
    downloaded_at: datetime | None
    error: str | None = None


def write_dataset(
    data_dir: Path,
    area_files: dict[str, list[str]],
    *,
    downloaded_at: datetime,
    san: str | None,
    complete: bool = True,
) -> DatasetManifest:
    """Materialize a dataset directory (area files + manifest). Shared by refresh
    and tests so checksums are computed one way."""
    ac_dir = data_dir / _AREACODES_DIR
    ac_dir.mkdir(parents=True, exist_ok=True)

    files_meta: dict[str, dict] = {}
    for area_code, numbers in area_files.items():
        body = _serialize_numbers(sorted(set(numbers)))
        (ac_dir / f"{area_code}.txt").write_bytes(body)
        files_meta[area_code] = {"checksum": _sha256(body), "count": len(set(numbers))}

    manifest = DatasetManifest(
        version=VERSION,
        downloaded_at=downloaded_at,
        san_fingerprint=san_fingerprint(san),
        area_codes=tuple(sorted(area_files)),
        complete=complete,
        files=files_meta,
    )
    payload = {
        "version": manifest.version,
        "downloaded_at": manifest.downloaded_at.isoformat(),
        "san_fingerprint": manifest.san_fingerprint,
        "area_codes": list(manifest.area_codes),
        "complete": manifest.complete,
        "files": manifest.files,
    }
    (data_dir / _MANIFEST_NAME).write_text(json.dumps(payload, indent=2), "utf-8")
    return manifest


def _atomic_swap(staging: Path, active: Path) -> None:
    """Install `staging` as `active`, preserving the prior `active` if the swap
    itself fails. Never leaves the previous dataset destroyed on failure."""
    backup = active.with_name(active.name + ".backup")
    if backup.exists():
        shutil.rmtree(backup)
    if active.exists():
        active.rename(backup)
    try:
        staging.rename(active)
    except Exception:
        if backup.exists():
            backup.rename(active)  # restore last-good
        raise
    if backup.exists():
        shutil.rmtree(backup)


async def refresh_registry(
    *,
    data_directory: str | Path,
    area_codes: frozenset[str] | set[str],
    san: str,
    downloader: RegistryDownloader,
    now: datetime | None = None,
) -> RefreshResult:
    """Download every subscribed area code and atomically replace the dataset.

    All-or-nothing: if any area code fails to download or validate, the active
    dataset is left exactly as it was and no partial dataset is ever installed.
    """
    now = now or datetime.now(timezone.utc)
    active = Path(data_directory)
    codes = sorted(area_codes)

    if not san:
        return RefreshResult(False, tuple(codes), 0, None, error="no SAN configured")
    if not codes:
        return RefreshResult(False, (), 0, None, error="no subscribed area codes")

    staging = active.with_name(active.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)

    try:
        area_files: dict[str, list[str]] = {}
        for area_code in codes:
            raw = await downloader.download_area_code(area_code)
            # Validate before trusting: an error page or truncated file raises here.
            area_files[area_code] = _parse_flat_file(raw, area_code)

        manifest = write_dataset(staging, area_files, downloaded_at=now, san=san, complete=True)
        _atomic_swap(staging, active)

        total = sum(v["count"] for v in manifest.files.values())
        logger.info(
            "national DNC dataset refreshed: %d area codes, %d numbers, san=%s",
            len(codes), total, manifest.san_fingerprint,
        )
        return RefreshResult(True, tuple(codes), total, now)
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        # active was never touched before the swap, so the last valid dataset stands.
        logger.error("national DNC refresh failed (last valid dataset preserved): %s", exc)
        return RefreshResult(False, tuple(codes), 0, None, error=str(exc))


async def refresh_from_settings(settings, downloader: RegistryDownloader | None = None) -> RefreshResult:
    downloader = downloader or FtcRegistryDownloader.from_settings(settings)
    return await refresh_registry(
        data_directory=settings.dnc_data_directory,
        area_codes=parse_area_codes(settings.dnc_area_codes),
        san=settings.dnc_san,
        downloader=downloader,
    )
