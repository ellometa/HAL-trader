"""Fair Value Gap (3-candle imbalance) detector.

Geometric definition only: only the wicks of candle i-2 and candle i are
inspected; candle i-1 can be any shape. Stricter variants additionally
require candle i-1 to have an impulsive body — we don't, since dropping
that constraint produces more candidates and the LLM can weigh them.
"""
from typing import Any

import pandas as pd

_REQUIRED_COLS = {"timestamp", "open", "high", "low", "close", "volume"}


def _validate(df: pd.DataFrame) -> None:
    missing = _REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")


def detect_fvgs(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Detect bullish/bearish FVGs over the input candles.

    Input  : DataFrame with timestamp/open/high/low/close/volume, oldest first.
    Output : list of dicts: type, start_index, end_index, start_time, end_time,
             price_low, price_high, mitigated, meta.
    """
    _validate(df)
    n = len(df)
    if n < 3:
        return []

    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    ts = df["timestamp"]

    fvgs: list[dict[str, Any]] = []
    for i in range(2, n):
        if low[i] > high[i - 2]:
            fvgs.append(
                _make(ts, "fvg_bullish", i - 2, i, float(high[i - 2]), float(low[i]))
            )
        elif high[i] < low[i - 2]:
            fvgs.append(
                _make(ts, "fvg_bearish", i - 2, i, float(high[i]), float(low[i - 2]))
            )

    # Mitigation pass: a later candle's range overlapping [price_low, price_high]
    # marks the FVG mitigated. Standard 1D interval overlap.
    for fvg in fvgs:
        for m in range(fvg["end_index"] + 1, n):
            if low[m] <= fvg["price_high"] and high[m] >= fvg["price_low"]:
                fvg["mitigated"] = True
                break

    return fvgs


def _make(
    ts: pd.Series, kind: str, start: int, end: int, price_low: float, price_high: float
) -> dict[str, Any]:
    return {
        "type": kind,
        "start_index": start,
        "end_index": end,
        "start_time": ts.iloc[start].isoformat(),
        "end_time": ts.iloc[end].isoformat(),
        "price_low": price_low,
        "price_high": price_high,
        "mitigated": False,
        "meta": {},
    }
