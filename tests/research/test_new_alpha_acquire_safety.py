"""Acquisition integrity, normalization semantics, and the safety guard.

Network-free: `_get` is replaced by fakes; normalization reads zips written
into tmp_path.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pandas as pd
import pytest
from studies.crypto_new_alpha import acquire, normalize


@pytest.fixture
def target(tmp_path):
    return acquire.Target(
        data_type="metrics",
        symbol="BTCUSDT",
        period="2024-01-01",
        url="https://example.invalid/BTCUSDT-metrics-2024-01-01.zip",
        path=tmp_path / "metrics" / "BTCUSDT" / "BTCUSDT-metrics-2024-01-01.zip",
    )


def _serve(monkeypatch, *, body: bytes | None, digest: str | None, counter: list[str]):
    def fake_get(url: str, timeout: float = 60.0) -> bytes | None:
        counter.append(url)
        if url.endswith(".CHECKSUM"):
            if digest is None:
                return None
            return f"{digest}  file.zip\n".encode()
        return body

    monkeypatch.setattr(acquire, "_get", fake_get)


class TestAcquireIntegrity:
    def test_valid_download_is_promoted_and_recorded(self, monkeypatch, target):
        body = b"payload-bytes"
        digest = hashlib.sha256(body).hexdigest()
        fetched: list[str] = []
        _serve(monkeypatch, body=body, digest=digest, counter=fetched)
        record = acquire.acquire_one(target)
        assert record.status == "downloaded"
        assert target.path.read_bytes() == body
        assert record.local_sha256 == digest

    def test_checksum_mismatch_is_rejected_and_nothing_promoted(self, monkeypatch, target):
        body = b"truncated"
        _serve(monkeypatch, body=body, digest="0" * 64, counter=[])
        record = acquire.acquire_one(target)
        assert record.status == "checksum-mismatch"
        assert not target.path.exists()
        assert not target.path.with_suffix(".zip.part").exists()

    def test_resume_skips_valid_file_without_body_fetch(self, monkeypatch, target):
        body = b"payload-bytes"
        digest = hashlib.sha256(body).hexdigest()
        target.path.parent.mkdir(parents=True)
        target.path.write_bytes(body)
        fetched: list[str] = []
        _serve(monkeypatch, body=b"SHOULD-NOT-BE-FETCHED", digest=digest, counter=fetched)
        record = acquire.acquire_one(target)
        assert record.status == "skipped-valid"
        assert fetched == [target.checksum_url]
        assert target.path.read_bytes() == body

    def test_absent_sidecar_records_absent(self, monkeypatch, target):
        _serve(monkeypatch, body=None, digest=None, counter=[])
        record = acquire.acquire_one(target)
        assert record.status == "absent"
        assert not target.path.exists()


def _zip_bytes(csv: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("data.csv", csv)
    return buffer.getvalue()


class TestLiquidationSemantics:
    def test_side_mapping_and_notional_conversion(self, tmp_path, monkeypatch):
        monkeypatch.setattr(normalize, "RAW_DIR", tmp_path)
        directory = tmp_path / "liquidations-cm" / "BTCUSD_PERP"
        directory.mkdir(parents=True)
        csv = (
            "time,side,order_type,time_in_force,original_quantity,price,"
            "average_price,order_status,last_fill_quantity,accumulated_fill_quantity\n"
            "1687656471926,SELL,LIMIT,IOC,7,30741.3,30631.6,FILLED,6,7\n"
            "1687656471926,SELL,LIMIT,IOC,7,30741.3,30631.6,FILLED,6,7\n"
            "1687656480000,BUY,LIMIT,IOC,3,30800.0,30810.5,FILLED,3,3\n"
        )
        (directory / "BTCUSD_PERP-liquidationSnapshot-2023-06-25.zip").write_bytes(_zip_bytes(csv))
        frame, audit = normalize.normalize_cm_liquidations("BTCUSD_PERP")
        # Exact archive duplicates are dropped and counted.
        assert audit["exact_duplicates_dropped"] == 1
        assert len(frame) == 2
        # side SELL = long force-closed; BUY = short force-closed.
        long_liq = frame.loc[frame["side"] == "SELL"].iloc[0]
        short_liq = frame.loc[frame["side"] == "BUY"].iloc[0]
        # BTCUSD_PERP contract = $100: notional is contracts * 100.
        assert long_liq["notional_usd"] == pytest.approx(700.0)
        assert short_liq["notional_usd"] == pytest.approx(300.0)
        assert str(frame["event_ts"].dt.tz) == "UTC"


class TestFlowNormalizationSemantics:
    def test_kline_grid_alignment_enforced_and_knowable_stamped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(normalize, "RAW_DIR", tmp_path)
        directory = tmp_path / "klines" / "BTCUSDT"
        directory.mkdir(parents=True)
        open_ms = 1577836800000  # 2020-01-01 00:00 UTC
        rows = []
        for index in range(4):
            start = open_ms + index * 900_000
            end = start + 900_000 - 1
            rows.append(f"{start},100,101,99,100.5,10,{end},1000,5,6,600,0")
        (directory / "BTCUSDT-15m-2020-01.zip").write_bytes(_zip_bytes("\n".join(rows) + "\n"))
        frame, audit = normalize.normalize_flow("BTCUSDT")
        assert audit["rows"] == 4
        assert audit["missing_intervals"] == 0
        # bar_close = open + 15m - 1ms, so knowable_at = bar_close + 1ms lands
        # exactly on the decision boundary (open + 15m), as in the prior pilot.
        expected = pd.Timestamp("2020-01-01 00:30:00", tz="UTC")
        assert frame["knowable_at"].iloc[1] == expected

    def test_misaligned_kline_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(normalize, "RAW_DIR", tmp_path)
        directory = tmp_path / "klines" / "BTCUSDT"
        directory.mkdir(parents=True)
        row = "1577836800000,100,101,99,100.5,10,1577837699000,1000,5,6,600,0"  # close 1s early
        (directory / "BTCUSDT-15m-2020-01.zip").write_bytes(_zip_bytes(row + "\n"))
        with pytest.raises(ValueError, match="grid"):
            normalize.normalize_flow("BTCUSDT")

    def test_impossible_flow_accounting_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(normalize, "RAW_DIR", tmp_path)
        directory = tmp_path / "klines" / "BTCUSDT"
        directory.mkdir(parents=True)
        # taker-buy quote volume exceeding total quote volume is impossible
        row = "1577836800000,100,101,99,100.5,10,1577837699999,1000,5,6,2000,0"
        (directory / "BTCUSDT-15m-2020-01.zip").write_bytes(_zip_bytes(row + "\n"))
        with pytest.raises(ValueError, match="impossible"):
            normalize.normalize_flow("BTCUSDT")


class TestOiNormalizationSemantics:
    def test_duplicates_dropped_and_conflicts_keep_last(self, tmp_path, monkeypatch):
        monkeypatch.setattr(normalize, "RAW_DIR", tmp_path)
        directory = tmp_path / "metrics" / "BTCUSDT"
        directory.mkdir(parents=True)
        csv = (
            "create_time,symbol,sum_open_interest,sum_open_interest_value,"
            "count_toptrader_long_short_ratio,sum_toptrader_long_short_ratio,"
            "count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
            "2024-01-01 00:00:00,BTCUSDT,100,5000000,1,1,1,1\n"
            "2024-01-01 00:00:00,BTCUSDT,100,5000000,1,1,1,1\n"
            "2024-01-01 00:05:00,BTCUSDT,101,5100000,1,1,1,1\n"
            "2024-01-01 00:05:00,BTCUSDT,102,5200000,1,1,1,1\n"
        )
        (directory / "BTCUSDT-metrics-2024-01-01.zip").write_bytes(_zip_bytes(csv))
        frame, audit = normalize.normalize_oi("BTCUSDT")
        assert audit["exact_duplicates_dropped"] == 1
        assert audit["conflicting_duplicates_kept_last"] == 1
        assert len(frame) == 2
        assert frame["oi_notional"].iloc[1] == pytest.approx(5_200_000.0)
        # The conservative publication charge is stamped on every row.
        lag = frame["knowable_at"] - frame["create_time"]
        assert (lag == pd.Timedelta("5min")).all()


class TestSafetyGuard:
    def test_study_package_names_no_order_mutation_symbol(self):
        import pathlib

        import studies.crypto_new_alpha as package

        root = pathlib.Path(package.__file__).parent
        forbidden = (
            "submit_order",
            "place_order",
            "cancel_order",
            "replace_order",
            "TradingClient",
        )
        for path in sorted(root.glob("*.py")):
            source = path.read_text()
            for symbol in forbidden:
                assert symbol not in source, f"{path.name} names {symbol}"

    def test_study_package_imports_no_trading_module(self):
        import pathlib

        import studies.crypto_new_alpha as package

        root = pathlib.Path(package.__file__).parent
        forbidden_imports = (
            "autotrader.execution",
            "autotrader.risk",
            "autotrader.reconciliation",
            "autotrader.runtime.live",
            "autotrader.state",
        )
        for path in sorted(root.glob("*.py")):
            source = path.read_text()
            for module in forbidden_imports:
                assert f"import {module}" not in source and f"from {module}" not in source, (
                    f"{path.name} imports {module}"
                )
