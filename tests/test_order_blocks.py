from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.ict.order_blocks import detect_order_blocks


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
    assert detect_order_blocks(_df(rows)) == []


def test_no_swings_means_no_bos():
    # Identical bars: with strict-greater swing detection, plateaus aren't
    # swings, so nothing to break.
    rows = [(100, 102, 99, 101)] * 10
    assert detect_order_blocks(_df(rows)) == []


def test_bullish_order_block():
    # swing high at idx 2 (h=110, neighbors h<=109), then a recovery,
    # then a down candle at idx 6 (the OB), then BOS at idx 7 (close=114>110)
    rows = [
        (97, 100, 95, 99),     # 0
        (99, 104, 96, 102),    # 1
        (102, 110, 100, 108),  # 2  swing high (110)
        (108, 109, 103, 104),  # 3  down
        (104, 105, 100, 101),  # 4  down — confirms swing
        (101, 106, 100, 105),  # 5  up
        (105, 106, 99, 100),   # 6  DOWN  ← OB candidate
        (100, 115, 99, 114),   # 7  BOS: close 114 > 110
    ]
    obs = detect_order_blocks(_df(rows))
    assert len(obs) == 1
    ob = obs[0]
    assert ob["type"] == "ob_bullish"
    assert ob["start_index"] == 6
    assert ob["end_index"] == 7
    assert ob["price_low"] == 99.0   # low of candle 6
    assert ob["price_high"] == 106.0  # high of candle 6
    assert ob["mitigated"] is False
    assert ob["meta"]["bos_index"] == 7


def test_bearish_order_block():
    # swing low at idx 2 (l=90), up candle at idx 6 (the OB), BOS at idx 7
    rows = [
        (103, 104, 100, 101),  # 0
        (101, 102, 96, 98),    # 1
        (98, 99, 90, 92),      # 2  swing low (90)
        (92, 96, 91, 95),      # 3  up
        (95, 100, 95, 99),     # 4  up — confirms swing
        (99, 100, 94, 95),     # 5  down
        (95, 101, 94, 100),    # 6  UP  ← OB candidate
        (100, 101, 85, 86),    # 7  BOS: close 86 < 90
    ]
    obs = detect_order_blocks(_df(rows))
    assert len(obs) == 1
    ob = obs[0]
    assert ob["type"] == "ob_bearish"
    assert ob["start_index"] == 6
    assert ob["end_index"] == 7
    assert ob["price_low"] == 94.0
    assert ob["price_high"] == 101.0
    assert ob["mitigated"] is False


def test_mitigated_bullish_ob():
    # Bullish OB scenario from above, plus a candle 8 wicking into [99, 106].
    rows = [
        (97, 100, 95, 99),
        (99, 104, 96, 102),
        (102, 110, 100, 108),
        (108, 109, 103, 104),
        (104, 105, 100, 101),
        (101, 106, 100, 105),
        (105, 106, 99, 100),
        (100, 115, 99, 114),
        (114, 115, 100, 110),  # low=100 <= price_high=106 -> mitigated
    ]
    obs = detect_order_blocks(_df(rows))
    assert len(obs) == 1
    assert obs[0]["mitigated"] is True
