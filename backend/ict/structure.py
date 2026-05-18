"""Market-structure detector.

BOS (Break of Structure): close beyond the most recent unbroken swing in
the direction of the prevailing trend. Continuation signal.

CHoCH (Change of Character): close beyond the most recent unbroken swing
*against* the prevailing trend. Reversal signal — the leg that flips the
trend.

Trend state is bootstrapped neutrally: the first swing break (either
direction) is classified as a BOS and seeds the trend. Subsequent breaks
in the same direction stay BOS; opposite-direction breaks become CHoCH
and flip the trend.

Also exposes ``find_swings`` — the pivot helper shared by structure,
order blocks, and liquidity-sweep detection.
"""
from typing import Any

import pandas as pd

_REQUIRED_COLS = {"timestamp", "open", "high", "low", "close", "volume"}


def _validate(df: pd.DataFrame) -> None:
    missing = _REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")


def find_swings(
    df: pd.DataFrame, n: int = 2
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (swing_highs, swing_lows) as lists of (index, price).

    Strict inequality on both sides: plateaus aren't swings.
    """
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    N = len(df)
    swing_highs: list[tuple[int, float]] = []
    swing_lows: list[tuple[int, float]] = []
    for k in range(n, N - n):
        if all(high[k] > high[k - j] for j in range(1, n + 1)) and all(
            high[k] > high[k + j] for j in range(1, n + 1)
        ):
            swing_highs.append((k, float(high[k])))
        if all(low[k] < low[k - j] for j in range(1, n + 1)) and all(
            low[k] < low[k + j] for j in range(1, n + 1)
        ):
            swing_lows.append((k, float(low[k])))
    return swing_highs, swing_lows


def detect_structure(df: pd.DataFrame, swing_n: int = 2) -> list[dict[str, Any]]:
    """Detect BOS / CHoCH events in chronological order."""
    _validate(df)
    n = len(df)
    if n < 2 * swing_n + 2:
        return []

    swing_highs, swing_lows = find_swings(df, swing_n)
    close = df["close"].to_numpy()
    ts = df["timestamp"]

    events: list[dict[str, Any]] = []
    broken_highs: set[int] = set()
    broken_lows: set[int] = set()
    trend = "neutral"  # "bullish" | "bearish" | "neutral"

    for i in range(n):
        active_sh = [
            (idx, p)
            for idx, p in swing_highs
            if idx + swing_n <= i and idx not in broken_highs
        ]
        if active_sh:
            sh_idx, sh_price = active_sh[-1]
            if close[i] > sh_price:
                broken_highs.add(sh_idx)
                kind = "choch_bullish" if trend == "bearish" else "bos_bullish"
                trend = "bullish"
                events.append(_make_event(ts, kind, sh_idx, i, sh_price))

        active_sl = [
            (idx, p)
            for idx, p in swing_lows
            if idx + swing_n <= i and idx not in broken_lows
        ]
        if active_sl:
            sl_idx, sl_price = active_sl[-1]
            if close[i] < sl_price:
                broken_lows.add(sl_idx)
                kind = "choch_bearish" if trend == "bullish" else "bos_bearish"
                trend = "bearish"
                events.append(_make_event(ts, kind, sl_idx, i, sl_price))

    return events


def _make_event(
    ts: pd.Series, kind: str, swing_idx: int, break_idx: int, price: float
) -> dict[str, Any]:
    return {
        "type": kind,
        "swing_index": swing_idx,
        "break_index": break_idx,
        "swing_time": ts.iloc[swing_idx].isoformat(),
        "break_time": ts.iloc[break_idx].isoformat(),
        "price": price,
        "meta": {},
    }
