"""hithink 桥接层测试（不依赖真实 CLI：monkeypatch _cli 造导出文件）。"""

from __future__ import annotations

import os

import pandas as pd
import pytest

from quant_sim.data import hithink


@pytest.fixture
def fake_cli(monkeypatch, tmp_path):
    """伪造 db export：写一个含两标的的 parquet；伪造 query：返回覆盖信息。"""

    def fake(args, timeout=300):
        if args[:2] == ["db", "export"]:
            out = args[args.index("--output") + 1]
            rows = []
            for code, base in [("600519.SH", 1500.0), ("000001.SZ", 10.0)]:
                dates = pd.bdate_range("2024-01-01", periods=10)
                for i, d in enumerate(dates):
                    px = base + i * 0.1
                    rows.append({"thscode": code, "date": str(d.date()), "open": px, "high": px * 1.01,
                                 "low": px * 0.99, "close": px, "volume": 1e6, "amount": px * 1e6})
            pd.DataFrame(rows).to_parquet(out)
            return {"path": out, "count": len(rows)}
        if args[:2] == ["db", "query"]:
            return [{"d0": "2016-08-29", "d1": "2026-08-28", "symbols": 5551, "rows": 10_000_000}]
        raise AssertionError(args)

    monkeypatch.setattr(hithink, "_cli", fake)
    return tmp_path


def test_export_daily_contract(fake_cli):
    out = os.path.join(str(fake_cli), "daily")
    written = hithink.export_daily(["600519.SH", "000001"], start="2024-01-01", out_dir=out)
    assert set(written) == {"600519", "000001"}
    df = pd.read_parquet(written["600519"])
    for col in ["open", "high", "low", "close", "volume", "amount", "pre_close"]:
        assert col in df.columns
    assert len(df) == 10
    # pre_close = 连续复权序列的上一收盘
    assert df["pre_close"].iloc[1] == pytest.approx(df["close"].iloc[0])
    assert pd.isna(df["pre_close"].iloc[0])
    # 导出目录可直接被平台 loader 读回
    from quant_sim.data.loader import load_panel

    panel = load_panel(out)
    assert panel.n_symbols == 2
    assert panel.history("600519", panel.dates[-1]) is not None


def test_export_missing_symbol(fake_cli, capsys):
    written = hithink.export_daily(["600519", "999999"], start="2024-01-01",
                                   out_dir=os.path.join(str(fake_cli), "d2"))
    assert "600519" in written and "999999" not in written
    assert "999999" in capsys.readouterr().out


def test_bad_adjust(fake_cli):
    with pytest.raises(ValueError):
        hithink.export_daily(["600519"], adjust="xxx", out_dir=".")


def _bj_ms(date_str: str) -> int:
    return int(pd.Timestamp(date_str, tz="Asia/Shanghai").timestamp() * 1000)


def test_etf_timezone_and_calendar_filter(monkeypatch, tmp_path):
    """date_ms 为北京零点：必须映射回北京交易日，且非开市日行被剔除。"""
    items = [
        {"date_ms": _bj_ms("2024-10-08"), "open_price": 0.95, "high_price": 1.14,
         "low_price": 0.95, "close_price": 1.14, "volume": 1e8, "turnover": 1e8},
        {"date_ms": _bj_ms("2024-10-09"), "open_price": 1.14, "high_price": 1.14,
         "low_price": 1.09, "close_price": 1.096, "volume": 1e8, "turnover": 1e8},
        # 周日假行（厂商偶尔返回）
        {"date_ms": _bj_ms("2024-10-13"), "open_price": 1.0, "high_price": 1.0,
         "low_price": 1.0, "close_price": 1.0, "volume": 1.0, "turnover": 1.0},
    ]

    def fake(args, timeout=300):
        if args[0] == "fund":
            return {"item": items}
        if args[:2] == ["db", "query"]:
            pad = [{"date": str(d.date())} for d in pd.bdate_range("2015-01-05", periods=600)]
            return pad + [{"date": d} for d in ("2024-10-08", "2024-10-09", "2024-10-10")]
        raise AssertionError(args)

    monkeypatch.setattr(hithink, "_cli", fake)
    monkeypatch.setattr(hithink._cn_trading_dates, "_cache", None, raising=False)
    out = str(tmp_path / "daily")
    written = hithink.export_etf_daily(["588000"], start="2024-10-01", end="2024-10-11", out_dir=out)
    monkeypatch.setattr(hithink._cn_trading_dates, "_cache", None, raising=False)
    df = pd.read_parquet(written["588000"])
    assert [str(d.date()) for d in df.index] == ["2024-10-08", "2024-10-09"]
    assert df["pre_close"].iloc[1] == pytest.approx(df["close"].iloc[0])
