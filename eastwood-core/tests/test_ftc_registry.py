"""
Official FTC National DNC Registry provider.

A number can only be reported `clear` against a complete, in-scope, fresh,
subscription-matched dataset. Every other condition — stale, out of scope,
partial, corrupt, unverifiable subscription — is `unknown`. `blocked` is
reported from any loadable listing, even a stale one.

Refresh is all-or-nothing: a failed download never installs a partial dataset
and never destroys the last valid one. No secret or registry number is ever
logged.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from domain.compliance import (
    BLOCKED,
    CLEAR,
    UNKNOWN,
    FtcRegistryScrubProvider,
    RegistryDownloader,
    is_dialable,
    refresh_registry,
    san_fingerprint,
    write_dataset,
)
from domain.compliance.ftc_registry import DatasetManifest

SAN = "SAN-SECRET-0001"
SAN_FP = san_fingerprint(SAN)
# Relative to the real clock so "fresh" means age 0 regardless of the wall date.
NOW = datetime.now(timezone.utc)

LISTED = "3135550000"        # on the registry, area 313
ABSENT = "3135551111"        # not on the registry, area 313
LISTED_E164 = "+13135550000"
ABSENT_E164 = "+13135551111"
OUT_OF_SCOPE_E164 = "+12485550000"   # area 248 (valid), never downloaded


def _provider(
    data_dir: Path,
    *,
    area_codes=("313",),
    max_age_days=31,
    san=SAN,
) -> FtcRegistryScrubProvider:
    return FtcRegistryScrubProvider(
        data_directory=data_dir,
        subscribed_area_codes=frozenset(area_codes),
        max_age_days=max_age_days,
        configured_san_fingerprint=san_fingerprint(san),
    )


def _install(data_dir: Path, *, downloaded_at=NOW, san=SAN, complete=True, area_files=None):
    area_files = area_files or {"313": [LISTED, "3135550002"]}
    return write_dataset(data_dir, area_files, downloaded_at=downloaded_at, san=san, complete=complete)


# ── Blocked ───────────────────────────────────────────────────────────────────

async def test_a_listed_number_is_blocked(tmp_path):
    _install(tmp_path)
    result = await _provider(tmp_path).scrub(LISTED_E164)

    assert result.status == BLOCKED
    assert result.provider == "ftc_registry"
    assert result.audit["reason"] == "listed"
    assert result.audit["area_code"] == "313"
    assert result.audit["san_fingerprint"] == SAN_FP  # fingerprint, never raw SAN


async def test_a_listed_number_is_blocked_even_from_a_stale_dataset(tmp_path):
    """Staleness makes a list under-report, never over-report. A hit still holds."""
    _install(tmp_path, downloaded_at=NOW - timedelta(days=90))
    result = await _provider(tmp_path).scrub(LISTED_E164)

    assert result.status == BLOCKED


# ── Clear only under a complete, fresh, in-scope, matched dataset ─────────────

async def test_an_absent_number_is_clear_with_a_fresh_complete_in_scope_dataset(tmp_path):
    _install(tmp_path, downloaded_at=NOW)
    provider = _provider(tmp_path)
    # scrub uses datetime.now internally; the dataset is dated ~now, so it is fresh.
    result = await provider.scrub(ABSENT_E164)

    assert result.status == CLEAR
    assert is_dialable(result.status)
    assert result.audit["reason"] == "absent_from_complete_dataset"
    assert result.audit["dataset_age_days"] <= 1  # fresh
    assert result.audit["checksum"].startswith("sha256:")


async def test_a_stale_dataset_cannot_clear(tmp_path):
    _install(tmp_path, downloaded_at=NOW - timedelta(days=40))
    result = await _provider(tmp_path, max_age_days=31).scrub(ABSENT_E164)

    assert result.status == UNKNOWN
    assert result.audit["reason"] == "stale_dataset"
    assert not is_dialable(result.status)


async def test_an_out_of_scope_area_code_is_unknown(tmp_path):
    _install(tmp_path)  # only 313 downloaded
    result = await _provider(tmp_path).scrub(OUT_OF_SCOPE_E164)

    assert result.status == UNKNOWN
    assert result.audit["reason"] == "out_of_scope"
    assert result.audit["area_code"] == "248"


async def test_an_incomplete_dataset_cannot_clear(tmp_path):
    _install(tmp_path, complete=False)
    result = await _provider(tmp_path).scrub(ABSENT_E164)

    assert result.status == UNKNOWN
    assert result.audit["reason"] == "incomplete_dataset"


async def test_a_dataset_from_a_different_subscription_cannot_clear(tmp_path):
    """The active dataset must belong to the configured SAN to attest clearance."""
    _install(tmp_path, san="SOME-OTHER-SAN")
    result = await _provider(tmp_path, san=SAN).scrub(ABSENT_E164)

    assert result.status == UNKNOWN
    assert result.audit["reason"] == "subscription_unverified"


async def test_no_dataset_at_all_is_unknown(tmp_path):
    result = await _provider(tmp_path).scrub(ABSENT_E164)

    assert result.status == UNKNOWN
    assert result.audit["reason"] == "no_dataset"


async def test_an_unparseable_number_is_unknown(tmp_path):
    _install(tmp_path)
    result = await _provider(tmp_path).scrub("not-a-number")

    assert result.status == UNKNOWN
    assert result.audit["reason"] == "unevaluable_number"


# ── Corruption after install ──────────────────────────────────────────────────

async def test_a_checksum_mismatch_is_unknown_not_trusted(tmp_path):
    _install(tmp_path)
    # Tamper with the on-disk file so it no longer matches the manifest checksum.
    listed_file = tmp_path / "areacodes" / "313.txt"
    listed_file.write_text(listed_file.read_text() + "3135559999\n")

    result = await _provider(tmp_path).scrub(ABSENT_E164)
    assert result.status == UNKNOWN
    assert result.audit["reason"] == "corrupt_dataset"


# ── Refresh: the download seam (a network double, not a fake provider) ─────────

class _FakeDownloader(RegistryDownloader):
    def __init__(self, data: dict[str, bytes], fail_on: frozenset[str] = frozenset()):
        self._data = data
        self._fail_on = fail_on

    async def download_area_code(self, area_code: str) -> bytes:
        if area_code in self._fail_on:
            raise RuntimeError(f"download failed for {area_code}")
        return self._data[area_code]


def _flat(*numbers: str) -> bytes:
    return ("\n".join(numbers) + "\n").encode()


async def test_refresh_installs_a_usable_dataset(tmp_path):
    data_dir = tmp_path / "dnc_registry"
    downloader = _FakeDownloader({"313": _flat(LISTED, "3135550002"), "248": _flat("2485550000")})

    result = await refresh_registry(
        data_directory=data_dir, area_codes={"313", "248"}, san=SAN, downloader=downloader, now=NOW
    )

    assert result.ok
    assert result.total_numbers == 3
    provider = _provider(data_dir, area_codes=("313", "248"))
    assert (await provider.scrub(LISTED_E164)).status == BLOCKED
    assert (await provider.scrub(ABSENT_E164)).status == CLEAR


async def test_a_partial_download_preserves_the_last_valid_dataset(tmp_path):
    data_dir = tmp_path / "dnc_registry"
    # First refresh succeeds and installs a good dataset.
    ok = await refresh_registry(
        data_directory=data_dir, area_codes={"313"}, san=SAN,
        downloader=_FakeDownloader({"313": _flat(LISTED)}), now=NOW,
    )
    assert ok.ok
    first_manifest = DatasetManifest.load(data_dir)

    # Second refresh fails on one area code.
    failed = await refresh_registry(
        data_directory=data_dir, area_codes={"313", "248"}, san=SAN,
        downloader=_FakeDownloader({"313": _flat(LISTED, "3135550003")}, fail_on=frozenset({"248"})),
        now=NOW + timedelta(days=1),
    )
    assert not failed.ok

    # The first dataset is untouched — same downloaded_at, same contents.
    after = DatasetManifest.load(data_dir)
    assert after.downloaded_at == first_manifest.downloaded_at
    assert after.files == first_manifest.files
    assert not (data_dir.with_name(data_dir.name + ".staging")).exists(), "no staging left behind"
    # And the good dataset still works.
    assert (await _provider(data_dir).scrub(LISTED_E164)).status == BLOCKED


async def test_a_corrupt_download_is_rejected_and_preserves_the_last_valid_dataset(tmp_path):
    data_dir = tmp_path / "dnc_registry"
    await refresh_registry(
        data_directory=data_dir, area_codes={"313"}, san=SAN,
        downloader=_FakeDownloader({"313": _flat(LISTED)}), now=NOW,
    )
    first = DatasetManifest.load(data_dir)

    # An HTML error page instead of a flat file must not be installed.
    failed = await refresh_registry(
        data_directory=data_dir, area_codes={"313"}, san=SAN,
        downloader=_FakeDownloader({"313": b"<html>Session expired</html>"}),
        now=NOW + timedelta(days=1),
    )
    assert not failed.ok
    assert DatasetManifest.load(data_dir).downloaded_at == first.downloaded_at


async def test_an_empty_download_is_rejected_as_partial(tmp_path):
    """An empty file means the download did not complete — never install it."""
    data_dir = tmp_path / "dnc_registry"
    result = await refresh_registry(
        data_directory=data_dir, area_codes={"313"}, san=SAN,
        downloader=_FakeDownloader({"313": b""}), now=NOW,
    )
    assert not result.ok
    assert DatasetManifest.load(data_dir) is None, "nothing should have been installed"


async def test_refresh_with_no_prior_dataset_leaves_no_dataset_on_failure(tmp_path):
    data_dir = tmp_path / "dnc_registry"
    failed = await refresh_registry(
        data_directory=data_dir, area_codes={"313"}, san=SAN,
        downloader=_FakeDownloader({}, fail_on=frozenset({"313"})), now=NOW,
    )
    assert not failed.ok
    # No dataset installed → the provider reports unknown, which is not dialable.
    result = await _provider(data_dir).scrub(ABSENT_E164)
    assert result.status == UNKNOWN
    assert not is_dialable(result.status)


async def test_refresh_refuses_without_a_san(tmp_path):
    result = await refresh_registry(
        data_directory=tmp_path / "d", area_codes={"313"}, san="",
        downloader=_FakeDownloader({"313": _flat(LISTED)}), now=NOW,
    )
    assert not result.ok
    assert "SAN" in result.error


# ── Secrets and registry data never reach the logs ────────────────────────────

async def test_no_secret_or_registry_number_appears_in_logs(tmp_path, caplog):
    data_dir = tmp_path / "dnc_registry"
    with caplog.at_level(logging.DEBUG):
        await refresh_registry(
            data_directory=data_dir, area_codes={"313"}, san=SAN,
            downloader=_FakeDownloader({"313": _flat(LISTED, ABSENT)}), now=NOW,
        )
        provider = _provider(data_dir)
        await provider.scrub(LISTED_E164)
        await provider.scrub(ABSENT_E164)

    text = caplog.text
    assert SAN not in text, "raw SAN must never be logged"
    assert LISTED not in text, "registry numbers must never be logged"
    assert ABSENT not in text
    # The non-secret fingerprint is fine to log.
    assert SAN_FP in text


async def test_persisted_audit_is_json_safe_and_holds_no_secret_or_numbers(tmp_path):
    """The audit dict is stored as JSONB — it must serialize and leak nothing."""
    _install(tmp_path)
    result = await _provider(tmp_path).scrub(LISTED_E164)

    blob = json.dumps(result.audit)  # must not raise
    assert SAN not in blob
    assert LISTED not in blob
    assert result.audit["san_fingerprint"] == SAN_FP
    assert result.audit["area_code_scope"] == ["313"]


# ── The gate still holds: unknown from this provider never queues ─────────────

async def test_unknown_from_ftc_provider_never_reaches_the_dial_queue(tmp_path, monkeypatch):
    """End to end: a stale dataset yields unknown, which enqueue_call refuses."""
    import workers.brain as brain

    _install(tmp_path, downloaded_at=NOW - timedelta(days=90))  # stale -> unknown for absent

    class _Lead:
        id = __import__("uuid").UUID("3f6c2b8e-9a41-4d6f-8f2b-7c1e5a904d33")
        phone_e164 = ABSENT_E164
        national_dnc_status = UNKNOWN

    dnc_writes: list = []
    enqueued: list = []

    class _LeadRepo:
        def __init__(self, s): pass
        async def get_by_id(self, _id): return _Lead()
        async def set_national_dnc(self, lead_id, status, **kw): dnc_writes.append(status)

    class _DQRepo:
        def __init__(self, s): pass
        async def enqueue(self, **kw): enqueued.append(kw)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _scope():
        yield object()

    monkeypatch.setattr(brain, "session_scope", _scope)
    monkeypatch.setattr(brain, "LeadRepo", _LeadRepo)
    monkeypatch.setattr(brain, "DialQueueRepo", _DQRepo)

    worker = brain.WorkersBrain()
    worker._dnc_scrub = _provider(tmp_path)
    try:
        status = await worker._ensure_national_dnc_status(object(), _Lead())
        await worker.enqueue_call(_Lead(), "default")
    finally:
        await worker._ai_agent.aclose()

    assert status == UNKNOWN
    assert enqueued == [], "an unknown lead must never be queued"
