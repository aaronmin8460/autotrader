"""Acquisition-integrity tests: resume, atomicity, checksum rejection.

These run before bulk acquisition because the mandate requires proving, not
assuming, that a restart neither redownloads valid data nor accepts a
truncated transfer. No test here touches the network: `_get` is replaced by
a fake whose bodies and failure modes the test controls.
"""

from __future__ import annotations

import hashlib

import pytest
from studies.crypto_funding_basis_pilot import acquire


@pytest.fixture
def target(tmp_path):
    return acquire.Target(
        data_type="fundingRate",
        symbol="BTCUSDT",
        period="2021-01",
        url="https://example.invalid/BTCUSDT-fundingRate-2021-01.zip",
        path=tmp_path / "fundingRate" / "BTCUSDT" / "BTCUSDT-fundingRate-2021-01.zip",
    )


def _serve(monkeypatch, *, body: bytes | None, digest: str | None, counter: list[str]):
    """Install a fake transport. `counter` records which URLs were fetched."""

    def fake_get(url: str, timeout: float = 60.0):
        counter.append(url)
        if url.endswith(".CHECKSUM"):
            if digest is None:
                return None
            return f"{digest}  file.zip\n".encode()
        return body

    monkeypatch.setattr(acquire, "_get", fake_get)


def test_downloads_and_promotes_atomically(monkeypatch, target):
    body = b"payload-bytes"
    digest = hashlib.sha256(body).hexdigest()
    fetched: list[str] = []
    _serve(monkeypatch, body=body, digest=digest, counter=fetched)

    record = acquire.acquire_one(target)

    assert record.status == "downloaded"
    assert record.local_sha256 == digest == record.provider_sha256
    assert target.path.read_bytes() == body
    assert record.size_bytes == len(body)
    assert not list(target.path.parent.glob("*.part")), "no .part may survive a success"


def test_valid_existing_file_is_skipped_without_body_fetch(monkeypatch, target):
    body = b"payload-bytes"
    digest = hashlib.sha256(body).hexdigest()
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(body)

    fetched: list[str] = []
    _serve(monkeypatch, body=body, digest=digest, counter=fetched)
    record = acquire.acquire_one(target)

    assert record.status == "skipped-valid"
    assert record.local_sha256 == digest
    assert fetched == [target.checksum_url], "a valid file must not refetch the body"


def test_truncated_body_is_rejected_and_leaves_no_final_file(monkeypatch, target):
    truthful = hashlib.sha256(b"the-whole-payload").hexdigest()
    fetched: list[str] = []
    _serve(monkeypatch, body=b"the-whole-pay", digest=truthful, counter=fetched)

    record = acquire.acquire_one(target)

    assert record.status == "checksum-mismatch"
    assert not target.path.exists(), "a truncated transfer must never be promoted"
    assert not list(target.path.parent.glob("*.part")), "the .part must be discarded"


def test_corrupt_existing_file_is_replaced_not_trusted(monkeypatch, target):
    body = b"good-bytes"
    digest = hashlib.sha256(body).hexdigest()
    target.path.parent.mkdir(parents=True)
    target.path.write_bytes(b"stale-and-wrong")

    fetched: list[str] = []
    _serve(monkeypatch, body=body, digest=digest, counter=fetched)
    record = acquire.acquire_one(target)

    assert record.status == "downloaded"
    assert target.path.read_bytes() == body


def test_stale_part_file_is_discarded_never_appended(monkeypatch, target):
    body = b"fresh-body"
    digest = hashlib.sha256(body).hexdigest()
    target.path.parent.mkdir(parents=True)
    part = target.path.with_suffix(target.path.suffix + ".part")
    part.write_bytes(b"leftover-garbage-from-a-killed-run")

    fetched: list[str] = []
    _serve(monkeypatch, body=body, digest=digest, counter=fetched)
    record = acquire.acquire_one(target)

    assert record.status == "downloaded"
    assert target.path.read_bytes() == body, "the stale prefix must not survive"


def test_absent_month_is_recorded_not_failed(monkeypatch, target):
    fetched: list[str] = []
    _serve(monkeypatch, body=None, digest=None, counter=fetched)

    record = acquire.acquire_one(target)

    assert record.status == "absent"
    assert not target.path.exists()


def test_month_sequence_is_inclusive_and_ordered():
    seq = acquire.months((2020, 11), (2021, 2))
    assert seq == ["2020-11", "2020-12", "2021-01", "2021-02"]


def test_target_inventory_covers_both_types_and_symbols():
    all_targets = acquire.targets()
    types = {t.data_type for t in all_targets}
    symbols = {t.symbol for t in all_targets}
    assert types == {"fundingRate", "premiumIndexKlines15m"}
    assert symbols == {"BTCUSDT", "ETHUSDT"}
    # Every premium URL must carry the interval segment the provider requires.
    premium = [t for t in all_targets if t.data_type == "premiumIndexKlines15m"]
    assert all("/premiumIndexKlines/" in t.url and "/15m/" in t.url for t in premium)
    # USD-margined perpetual family only.
    assert all("/futures/um/" in t.url for t in all_targets)


def test_symbol_map_is_the_recorded_perp_to_spot_mapping():
    assert acquire.SYMBOL_MAP == {"BTCUSDT": "BTC/USD", "ETHUSDT": "ETH/USD"}


def test_checksum_parser_accepts_sidecar_and_rejects_noise():
    digest = "a" * 64
    assert acquire.parse_checksum(f"{digest}  x.zip\n".encode()) == digest
    assert acquire.parse_checksum(b"") is None
    assert acquire.parse_checksum(b"not-a-digest x.zip") is None
