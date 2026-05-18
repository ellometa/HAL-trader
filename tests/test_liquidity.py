from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.ict.liquidity import detect_liquidity_sweeps


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    t0 = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return pd.DataFrame(
        {
            "timestamp": [t0 + timedelta(hours=i) for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100.0] * len(rows),
        }
    )


def test_too_short_returns_empty_list():
    rows = [(100, 102, 99, 101)] * 4
    assert detect_liquidity_sweeps(_df(rows)) == []


def test_no_swings_means_no_sweeps():
    rows = [(100, 102, 99, 101)] * 10
    assert detect_liquidity_sweeps(_df(rows)) == []


def test_bearish_sweep_above_swing_high():
    # Swing high 110 at idx 2, confirmed by idx 4. Bar 5 wicks to 112 then
    # closes 108 (back below 110) — bearish sweep.
    rows = [
        (97, 100, 95, 99),
        (99, 104, 96, 102),
        (102, 110, 100, 108),   # swing high 110
        (108, 109, 103, 104),
        (104, 105, 100, 101),
        (101, 112, 100, 108),   # wick 112 > 110, close 108 <= 110 -> sweep
    ]
    sweeps = detect_liquidity_sweeps(_df(rows))
    assert len(sweeps) == 1
    s = sweeps[0]
    assert s["type"] == "sweep_bearish"
    assert s["swing_index"] == 2
    assert s["sweep_index"] == 5
    assert s["level"] == 110.0
    assert s["wick"] == 112.0
    assert s["close"] == 108.0


def test_bullish_sweep_below_swing_low():
    rows = [
        (103, 104, 100, 101),
        (101, 102, 96, 98),
        (98, 99, 90, 92),       # swing low 90
        (92, 96, 91, 95),
        (95, 100, 95, 99),
        (99, 100, 88, 95),      # wick 88 < 90, close 95 >= 90 -> bullish sweep
    ]
    sweeps = detect_liquidity_sweeps(_df(rows))
    assert len(sweeps) == 1
    s = sweeps[0]
    assert s["type"] == "sweep_bullish"
    assert s["swing_index"] == 2
    assert s["level"] == 90.0
    assert s["wick"] == 88.0


def test_break_with_close_is_not_a_sweep():
    # Same setup as bearish sweep test, but bar 5 closes above the level —
    # that's structure (BOS), not a sweep.
    rows = [
        (97, 100, 95, 99),
        (99, 104, 96, 102),
        (102, 110, 100, 108),   # swing high 110
        (108, 109, 103, 104),
        (104, 105, 100, 101),
        (101, 115, 100, 114),   # close 114 > 110 -> break, not sweep
    ]
    assert detect_liquidity_sweeps(_df(rows)) == []


def test_bar_not_reaching_swing_is_not_a_sweep():
    rows = [
        (97, 100, 95, 99),
        (99, 104, 96, 102),
        (102, 110, 100, 108),   # swing high 110
        (108, 109, 103, 104),
        (104, 105, 100, 101),
        (101, 109, 100, 105),   # high 109 < 110 -> nothing
    ]
    assert detect_liquidity_sweeps(_df(rows)) == []
