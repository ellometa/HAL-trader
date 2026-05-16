from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.ict.fvg import detect_fvgs


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows: (open, high, low, close). 1h cadence starting 2024-01-01 UTC."""
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


def test_empty_df_returns_empty_list():
    assert detect_fvgs(_df([])) == []


def test_too_short_returns_empty_list():
    rows = [(100, 102, 99, 101), (101, 103, 100, 102)]
    assert detect_fvgs(_df(rows)) == []


def test_missing_columns_raises():
    bad = pd.DataFrame({"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]})
    try:
        detect_fvgs(bad)
    except ValueError:
        return
    raise AssertionError("expected ValueError for missing columns")


def test_clean_bullish_fvg():
    # gap between high[0]=102 and low[2]=103
    rows = [
        (100, 102, 99, 101),
        (101, 105, 100, 104),
        (104, 108, 103, 107),
    ]
    fvgs = detect_fvgs(_df(rows))
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f["type"] == "fvg_bullish"
    assert f["price_low"] == 102.0
    assert f["price_high"] == 103.0
    assert f["start_index"] == 0
    assert f["end_index"] == 2
    assert f["mitigated"] is False
    assert f["start_time"].startswith("2024-01-01T00:00:00")


def test_clean_bearish_fvg():
    # gap between low[0]=99 and high[2]=98
    rows = [
        (101, 102, 99, 100),
        (100, 100, 96, 97),
        (97, 98, 94, 95),
    ]
    fvgs = detect_fvgs(_df(rows))
    assert len(fvgs) == 1
    f = fvgs[0]
    assert f["type"] == "fvg_bearish"
    assert f["price_low"] == 98.0
    assert f["price_high"] == 99.0
    assert f["mitigated"] is False


def test_no_fvg_when_wicks_overlap():
    rows = [
        (100, 102, 99, 101),
        (101, 104, 100, 103),
        (103, 105, 101, 104),  # low=101 not > high[0]=102
    ]
    assert detect_fvgs(_df(rows)) == []


def test_mitigated_bullish_fvg():
    # bullish FVG from candles 0..2, then candle 3 wicks back into the zone
    rows = [
        (100, 102, 99, 101),
        (101, 105, 100, 104),
        (104, 108, 103, 107),
        (107, 108, 102, 104),  # low=102 <= price_high=103 -> mitigated
    ]
    fvgs = detect_fvgs(_df(rows))
    assert len(fvgs) == 1
    assert fvgs[0]["mitigated"] is True


def test_unmitigated_fvg_when_later_candle_above_zone():
    # FVG from candles 0..2; candle 3 stays entirely above the zone
    rows = [
        (100, 102, 99, 101),
        (101, 105, 100, 104),
        (104, 108, 103, 107),
        (107, 110, 105, 109),  # low=105 > price_high=103 -> not mitigated
    ]
    fvgs = detect_fvgs(_df(rows))
    assert len(fvgs) == 1
    assert fvgs[0]["mitigated"] is False
