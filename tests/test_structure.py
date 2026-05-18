from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.ict.structure import detect_structure, find_swings


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
    assert detect_structure(_df(rows)) == []


def test_no_swings_means_no_events():
    rows = [(100, 102, 99, 101)] * 10
    assert detect_structure(_df(rows)) == []


def test_first_swing_high_break_is_bos_bullish():
    # Swing high at idx 2 (h=110), confirmed at idx 4. Close 114 > 110 at idx 5.
    rows = [
        (97, 100, 95, 99),
        (99, 104, 96, 102),
        (102, 110, 100, 108),   # swing high (110)
        (108, 109, 103, 104),
        (104, 105, 100, 101),
        (101, 115, 100, 114),   # BOS bullish (first break, seeds trend)
    ]
    events = detect_structure(_df(rows))
    assert len(events) == 1
    e = events[0]
    assert e["type"] == "bos_bullish"
    assert e["swing_index"] == 2
    assert e["break_index"] == 5
    assert e["price"] == 110.0


def test_choch_bearish_after_bos_bullish():
    # First a bullish BOS (sets trend), then a swing-low break -> CHoCH bearish.
    rows = [
        (97, 100, 95, 99),
        (99, 104, 96, 102),
        (102, 110, 100, 108),   # swing high 110
        (108, 109, 103, 104),
        (104, 105, 100, 101),
        (101, 115, 100, 114),   # BOS bullish
        (114, 116, 105, 110),
        (110, 112, 95, 100),    # swing low candidate (95) at idx 7
        (100, 108, 98, 107),    # confirms swing low (low=98 > 95)
        (107, 110, 102, 109),   # confirms swing low (low=102 > 95)
        (109, 110, 90, 92),     # close 92 < 95 -> CHoCH bearish
    ]
    events = detect_structure(_df(rows))
    types = [e["type"] for e in events]
    assert "bos_bullish" in types
    assert "choch_bearish" in types
    # CHoCH should come after the BOS
    bos_idx = types.index("bos_bullish")
    choch_idx = types.index("choch_bearish")
    assert choch_idx > bos_idx


def test_consecutive_bullish_bos_stay_bos():
    # Two successive swing highs broken in sequence — both should be BOS bullish
    # since trend never flips bearish.
    rows = [
        (97, 100, 95, 99),
        (99, 104, 96, 102),
        (102, 110, 100, 108),   # swing high 110
        (108, 109, 103, 104),
        (104, 105, 100, 101),
        (101, 115, 100, 114),   # break 110 -> BOS bullish
        (114, 120, 113, 119),   # swing high 120
        (119, 119, 116, 117),
        (117, 118, 115, 116),
        (116, 117, 113, 114),
        (114, 130, 113, 129),   # break 120 -> BOS bullish (continuation)
    ]
    events = detect_structure(_df(rows))
    bullish_events = [e for e in events if e["type"] == "bos_bullish"]
    assert len(bullish_events) >= 2
    assert all(e["type"] != "choch_bullish" for e in events)


def test_find_swings_strict_inequality():
    rows = [(100, 102, 99, 101)] * 5
    highs, lows = find_swings(_df(rows), n=2)
    assert highs == []
    assert lows == []
