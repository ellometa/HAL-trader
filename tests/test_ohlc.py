import asyncio

import httpx
import pandas as pd
import pytest
import respx

from backend import ohlc


def test_is_crypto_routes_correctly():
    assert ohlc._is_crypto("BTCUSDT")
    assert ohlc._is_crypto("ETHBTC")
    assert ohlc._is_crypto("SOLUSDC")
    assert ohlc._is_crypto("BTCUSD")
    assert not ohlc._is_crypto("AAPL")
    # Known v0 limitation: EURUSD matches `[A-Z]+USD` and will (incorrectly)
    # route to Binance. Forex via yfinance needs the `=X` suffix, deferred
    # to phase 8 polish.
    assert ohlc._is_crypto("EURUSD")


def test_unknown_timeframe_raises():
    with pytest.raises(ohlc.TimeframeError):
        asyncio.run(ohlc.fetch_ohlc("BTCUSDT", "3h"))


def test_fetch_binance_shape_and_dtypes():
    base_ms = 1_700_000_000_000
    fake = []
    for i in range(3):
        open_ms = base_ms + i * 3_600_000
        close_ms = open_ms + 3_600_000 - 1
        fake.append(
            [
                open_ms, "100.0", "102.0", "99.5", "101.0", "12.34",
                close_ms, "0", 0, "0", "0", "0",
            ]
        )
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.binance.com/api/v3/klines").mock(
            return_value=httpx.Response(200, json=fake)
        )
        df = asyncio.run(ohlc.fetch_ohlc("BTCUSDT", "1h", 3))

    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 3
    assert df["open"].dtype == "float64"
    assert df["close"].dtype == "float64"
    assert df["volume"].dtype == "float64"
    assert "datetime64" in str(df["timestamp"].dtype)
    # Oldest-first ordering: timestamps strictly increasing.
    assert df["timestamp"].is_monotonic_increasing


def test_fetch_binance_empty_response_raises():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.binance.com/api/v3/klines").mock(
            return_value=httpx.Response(200, json=[])
        )
        with pytest.raises(RuntimeError, match="no data"):
            asyncio.run(ohlc.fetch_ohlc("BTCUSDT", "1h"))


def test_fetch_binance_4xx_surfaces_error():
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.binance.com/api/v3/klines").mock(
            return_value=httpx.Response(
                400, json={"code": -1121, "msg": "Invalid symbol."}
            )
        )
        with pytest.raises(RuntimeError, match="Invalid symbol"):
            asyncio.run(ohlc.fetch_ohlc("ZZZZUSDT", "1h"))


def test_fetch_yfinance_route_and_schema(monkeypatch):
    calls: dict[str, str] = {}

    def fake_yf_download(symbol: str, interval: str) -> pd.DataFrame:
        calls["symbol"] = symbol
        calls["interval"] = interval
        idx = pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True)
        df = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0],
                "High": [102.0, 103.0, 104.0],
                "Low": [99.0, 100.0, 101.0],
                "Close": [101.0, 102.0, 103.0],
                "Volume": [1000, 1100, 1200],
            },
            index=idx,
        )
        df.index.name = "Date"
        return df

    monkeypatch.setattr(ohlc, "_yf_download", fake_yf_download)
    df = asyncio.run(ohlc.fetch_ohlc("AAPL", "1D", 3))

    assert calls["symbol"] == "AAPL"
    assert calls["interval"] == "1d"
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 3
    assert df["timestamp"].is_monotonic_increasing
