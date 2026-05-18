"""Liquidity-sweep detector.

A sweep is a wick that pokes beyond a prior swing level *without* a close
beyond it — price grabs the resting stop-loss / breakout-entry liquidity
and rejects. Distinct from a Break of Structure (close-beyond), which
``structure.detect_structure`` handles.

Naming follows the bias implication:
- ``sweep_bearish``: wick above a swing high, close back inside. Buy-side
  liquidity taken; bearish rejection.
- ``sweep_bullish``: wick below a swing low, close back inside. Sell-side
  liquidity taken; bullish rejection.

A swing that closes beyond is *not* a sweep — it's structure. We skip
bars whose close confirms the break.
"""
from typing import Any

import pandas as pd

from backend.ict.structure import find_swings

_REQUIRED_COLS = {"timestamp", "open", "high", "low", "close", "volume"}


def _validate(df: pd.DataFrame) -> None:
    missing = _REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")


def detect_liquidity_sweeps(df: pd.DataFrame, swing_n: int = 2) -> list[dict[str, Any]]:
    _validate(df)
    n = len(df)
    if n < 2 * swing_n + 2:
        return []

    swing_highs, swing_lows = find_swings(df, swing_n)
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    ts = df["timestamp"]

    sweeps: list[dict[str, Any]] = []
    swept_highs: set[int] = set()
    swept_lows: set[int] = set()

    for i in range(n):
        active_sh = [
            (idx, p)
            for idx, p in swing_highs
            if idx + swing_n <= i and idx not in swept_highs and idx != i
        ]
        if active_sh:
            sh_idx, sh_price = active_sh[-1]
            if high[i] > sh_price and close[i] <= sh_price:
                swept_highs.add(sh_idx)
                sweeps.append(
                    _make_sweep(
                        ts, "sweep_bearish", sh_idx, i, sh_price,
                        wick=float(high[i]), close_price=float(close[i]),
                    )
                )

        active_sl = [
            (idx, p)
            for idx, p in swing_lows
            if idx + swing_n <= i and idx not in swept_lows and idx != i
        ]
        if active_sl:
            sl_idx, sl_price = active_sl[-1]
            if low[i] < sl_price and close[i] >= sl_price:
                swept_lows.add(sl_idx)
                sweeps.append(
                    _make_sweep(
                        ts, "sweep_bullish", sl_idx, i, sl_price,
                        wick=float(low[i]), close_price=float(close[i]),
                    )
                )

    return sweeps


def _make_sweep(
    ts: pd.Series,
    kind: str,
    swing_idx: int,
    sweep_idx: int,
    level: float,
    *,
    wick: float,
    close_price: float,
) -> dict[str, Any]:
    return {
        "type": kind,
        "swing_index": swing_idx,
        "sweep_index": sweep_idx,
        "swing_time": ts.iloc[swing_idx].isoformat(),
        "sweep_time": ts.iloc[sweep_idx].isoformat(),
        "level": level,
        "wick": wick,
        "close": close_price,
        "meta": {},
    }
