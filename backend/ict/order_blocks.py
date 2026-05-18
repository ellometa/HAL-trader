"""Order Block detector.

Bullish OB = last DOWN candle (close < open) before the impulsive up-move
that causes a Break of Structure (close beyond the most recent unbroken
swing high). Bearish OB is the mirror.

Zone choice: full wick range (low, high) of the OB candle. Body-only
(open–close) is a common stricter variant. Some practitioners additionally
require an FVG immediately after the OB to "confirm" it — we don't here.
More candidates returned; LLM weighs relevance.
"""
from typing import Any

import pandas as pd

from backend.ict.structure import find_swings

_REQUIRED_COLS = {"timestamp", "open", "high", "low", "close", "volume"}


def _validate(df: pd.DataFrame) -> None:
    missing = _REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing columns: {sorted(missing)}")


def detect_order_blocks(df: pd.DataFrame, swing_n: int = 2) -> list[dict[str, Any]]:
    """Detect order blocks confirmed by a BOS."""
    _validate(df)
    n = len(df)
    if n < 2 * swing_n + 2:
        return []

    swing_highs, swing_lows = find_swings(df, swing_n)
    open_ = df["open"].to_numpy()
    high = df["high"].to_numpy()
    low = df["low"].to_numpy()
    close = df["close"].to_numpy()
    ts = df["timestamp"]

    obs: list[dict[str, Any]] = []
    broken_highs: set[int] = set()
    broken_lows: set[int] = set()

    for i in range(n):
        # Active swing highs at candle i: confirmed (idx + swing_n <= i) and
        # not yet broken. Take the most recent — that's the level whose break
        # defines a Break of Structure.
        active_sh = [
            (idx, p)
            for idx, p in swing_highs
            if idx + swing_n <= i and idx not in broken_highs
        ]
        if active_sh:
            sh_idx, sh_price = active_sh[-1]
            if close[i] > sh_price:
                broken_highs.add(sh_idx)
                ob_idx = _last_opposing(open_, close, i - 1, sh_idx, want_down=True)
                if ob_idx is not None:
                    obs.append(_make_ob(ts, df, "ob_bullish", ob_idx, i))

        active_sl = [
            (idx, p)
            for idx, p in swing_lows
            if idx + swing_n <= i and idx not in broken_lows
        ]
        if active_sl:
            sl_idx, sl_price = active_sl[-1]
            if close[i] < sl_price:
                broken_lows.add(sl_idx)
                ob_idx = _last_opposing(open_, close, i - 1, sl_idx, want_down=False)
                if ob_idx is not None:
                    obs.append(_make_ob(ts, df, "ob_bearish", ob_idx, i))

    # Mitigation: only candles AFTER the BOS count. The candles between OB
    # and BOS are the impulsive move forming the structure — they shouldn't
    # mitigate the zone they just created.
    for ob in obs:
        for m in range(ob["end_index"] + 1, n):
            if low[m] <= ob["price_high"] and high[m] >= ob["price_low"]:
                ob["mitigated"] = True
                break

    return obs


def _last_opposing(
    open_, close, start: int, stop_exclusive: int, *, want_down: bool
) -> int | None:
    """Walk backwards from `start` down to (but not including) `stop_exclusive`,
    returning the index of the last candle matching the opposing direction.
    `want_down=True` → close < open; False → close > open.
    """
    for j in range(start, stop_exclusive, -1):
        if want_down and close[j] < open_[j]:
            return j
        if not want_down and close[j] > open_[j]:
            return j
    return None


def _make_ob(
    ts: pd.Series, df: pd.DataFrame, kind: str, ob_idx: int, bos_idx: int
) -> dict[str, Any]:
    return {
        "type": kind,
        "start_index": ob_idx,
        "end_index": bos_idx,
        "start_time": ts.iloc[ob_idx].isoformat(),
        "end_time": ts.iloc[bos_idx].isoformat(),
        "price_low": float(df["low"].iloc[ob_idx]),
        "price_high": float(df["high"].iloc[ob_idx]),
        "mitigated": False,
        "meta": {"bos_index": bos_idx},
    }
