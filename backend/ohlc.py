"""OHLC fetcher.

Routes crypto symbols to Binance's public REST API; everything else to
yfinance. Async I/O wrapper — no detection logic here. The output schema
matches what `backend.ict.*` detectors expect:
    columns = timestamp (datetime, UTC), open, high, low, close, volume (float)
    row order: oldest -> newest, integer-indexed 0..N-1.

TradingView sends formats like `BINANCE:BTCUSDT` or `NASDAQ:AAPL`. The
extension strips the exchange prefix before sending, so we receive bare
symbols here.
"""
import asyncio
import re
import time

import httpx
import pandas as pd
import yfinance as yf

# Crypto pair heuristic. Limitation: forex pairs like EURUSD match
# `[A-Z]+USD` and will route to Binance, where they don't exist and the
# request will fail. v0 lives with the failure; phase 8 polish can add a
# forex branch with the `=X` yfinance suffix.
_CRYPTO_PATTERNS = [
    re.compile(r"^[A-Z]{2,10}USDT?$"),
    re.compile(r"^[A-Z]{2,10}USDC$"),
    re.compile(r"^[A-Z]{2,10}BTC$"),
]

_BACKEND_TIMEFRAMES = {"1m", "5m", "15m", "1h", "4h", "1D", "1W"}

_BINANCE_TF = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1h": "1h", "4h": "4h",
    "1D": "1d", "1W": "1w",
}

# yfinance lacks a native 4h interval — resample from 1h. Documented in
# the resample branch of _fetch_yfinance.
_YF_TF = {
    "1m": "1m", "5m": "5m", "15m": "15m",
    "1h": "1h",
    "4h": "_RESAMPLE_FROM_1H",
    "1D": "1d", "1W": "1wk",
}

# yfinance accepts `period`, not bar counts. Over-fetch and trim. Intraday
# intervals have hard caps on yfinance history (e.g. 1m ~7 days); if the
# user asks for more, yfinance returns empty and we surface that.
_YF_PERIOD = {
    "1m": "5d",
    "5m": "30d",
    "15m": "60d",
    "1h": "730d",
    "1d": "max",
    "1wk": "max",
}


class TimeframeError(ValueError):
    """Raised when the requested timeframe isn't in the backend's normalized set."""


def _is_crypto(symbol: str) -> bool:
    return any(p.match(symbol) for p in _CRYPTO_PATTERNS)


# Process-local OHLC cache. TTL kills entries on the natural bar boundary
# (60s); within that window, repeat queries on the same chart skip the
# network. Keyed by (symbol, timeframe, limit) — limit varies almost never
# but including it avoids surprises if a caller changes it.
_CACHE_TTL = 60.0  # seconds
_cache: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}
_cache_stats = {"hits": 0, "misses": 0}


def cache_stats() -> dict[str, int]:
    """Cache hit/miss counters. Used by tests and the optional /stats endpoint."""
    return dict(_cache_stats)


def clear_cache() -> None:
    _cache.clear()
    _cache_stats["hits"] = 0
    _cache_stats["misses"] = 0


async def fetch_ohlc(symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    """Fetch up to `limit` most-recent candles for `symbol` at `timeframe`.

    Cached for ``_CACHE_TTL`` seconds. Returns a defensive copy so detector
    code can't mutate the cached frame.
    """
    if timeframe not in _BACKEND_TIMEFRAMES:
        raise TimeframeError(
            f"Unknown timeframe {timeframe!r}. Supported: {sorted(_BACKEND_TIMEFRAMES)}"
        )

    key = (symbol, timeframe, limit)
    now = time.monotonic()
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL:
        _cache_stats["hits"] += 1
        return cached[1].copy()

    _cache_stats["misses"] += 1
    if _is_crypto(symbol):
        df = await _fetch_binance(symbol, timeframe, limit)
    else:
        df = await _fetch_yfinance(symbol, timeframe, limit)
    _cache[key] = (now, df)
    return df.copy()


async def _fetch_binance(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    interval = _BINANCE_TF[timeframe]
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params)
    if resp.status_code != 200:
        try:
            msg = resp.json().get("msg", resp.text)
        except Exception:
            msg = resp.text
        raise RuntimeError(f"Binance error for {symbol} {timeframe}: {msg}")
    data = resp.json()
    if not data:
        raise RuntimeError(f"Binance returned no data for {symbol} {timeframe}")
    # kline row: [open_ms, open, high, low, close, volume, close_ms, ...].
    # We use close_ms as the canonical timestamp — marks the moment the candle
    # was finalized, less ambiguous than open-time when reasoning about
    # "what just happened" in user questions.
    raw = pd.DataFrame(
        data,
        columns=[
            "open_ms", "open", "high", "low", "close", "volume",
            "close_ms", "_qv", "_n", "_taker_base", "_taker_quote", "_ignore",
        ],
    )
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(raw["close_ms"], unit="ms", utc=True),
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "volume": raw["volume"].astype(float),
        }
    )


async def _fetch_yfinance(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    interval = _YF_TF[timeframe]
    if interval == "_RESAMPLE_FROM_1H":
        raw = await asyncio.to_thread(_yf_download, symbol, "1h")
        df = _normalize_yf(raw)
        df = (
            df.set_index("timestamp")
            .resample("4h")
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .dropna()
            .reset_index()
        )
        return df.tail(limit).reset_index(drop=True)
    raw = await asyncio.to_thread(_yf_download, symbol, interval)
    df = _normalize_yf(raw)
    return df.tail(limit).reset_index(drop=True)


def _yf_download(symbol: str, interval: str) -> pd.DataFrame:
    period = _YF_PERIOD.get(interval, "60d")
    raw = yf.download(
        symbol,
        period=period,
        interval=interval,
        progress=False,
        auto_adjust=False,
        threads=False,
    )
    if raw is None or raw.empty:
        raise RuntimeError(
            f"yfinance returned no data for {symbol} interval={interval} "
            f"period={period}. Note: intraday intervals have short history caps "
            f"(e.g. 1m ~ 7 days)."
        )
    return raw


def _normalize_yf(raw: pd.DataFrame) -> pd.DataFrame:
    # Single-ticker downloads can still return MultiIndex columns depending on
    # yfinance version / settings. Flatten by taking the field name.
    if isinstance(raw.columns, pd.MultiIndex):
        raw = raw.copy()
        raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
    raw = raw.reset_index().rename(columns=str.lower)
    ts_col = "datetime" if "datetime" in raw.columns else "date"
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(raw[ts_col], utc=True),
            "open": raw["open"].astype(float),
            "high": raw["high"].astype(float),
            "low": raw["low"].astype(float),
            "close": raw["close"].astype(float),
            "volume": raw["volume"].astype(float),
        }
    )
