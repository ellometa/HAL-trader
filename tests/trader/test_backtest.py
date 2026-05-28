"""Backtest harness over the deterministic rule policy (client=None, no
network). The same crafted bullish-OB scene the engine test uses is fed as
one continuous frame to ``run_backtest``; we assert a real trade is produced,
that the summary is coherent, and — the property that makes a backtest worth
anything — that no trade exits before (or on) the bar it entered on.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pandas as pd

from backend.trader.backtest import run_backtest
from backend.trader.config import load_config

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# Bullish order block (down candle at idx 6) confirmed by a BOS at idx 7,
# then an entry bar (8), a bar that pierces the stop (9), and a tail bar (10).
_OB_ROWS = [
    (97, 100, 95, 99),     # 0
    (99, 104, 96, 102),    # 1
    (102, 110, 100, 108),  # 2  swing high (110)
    (108, 109, 103, 104),  # 3
    (104, 105, 100, 101),  # 4
    (101, 106, 100, 105),  # 5
    (105, 106, 99, 100),   # 6  DOWN  <- bullish OB [99, 106]
    (100, 115, 99, 114),   # 7  BOS: close 114 > 110
    (113, 114, 109, 110),  # 8  retrace into the no-chase band -> entry @110
    (100, 100, 80, 82),    # 9  pierces stop (low 80 < OB low 99)
    (82, 84, 81, 83),      # 10 tail / final forming bar
]


def _df(rows):
    return pd.DataFrame(
        {
            "timestamp": [T0 + timedelta(hours=i) for i in range(len(rows))],
            "open": [r[0] for r in rows],
            "high": [r[1] for r in rows],
            "low": [r[2] for r in rows],
            "close": [r[3] for r in rows],
            "volume": [100.0] * len(rows),
        }
    )


def test_backtest_produces_trade_and_has_no_lookahead():
    cfg = load_config()
    df = _df(_OB_ROWS)
    result = asyncio.run(
        run_backtest(symbol="BTCUSDT", df=df, config=cfg, client=None, warmup=3)
    )

    # window/decision bookkeeping
    assert result["symbol"] == "BTCUSDT"
    assert result["policy"] == "rule"           # client=None forces the baseline
    assert result["bars"] == len(df)
    assert result["decisions"] == len(df) - 3 + 1

    # a real trade happened, and it was the stop-out
    assert result["closed_trades"] == 1
    assert result["losses"] == 1 and result["wins"] == 0
    trade = result["trades"][0]
    assert trade["reason"] == "stop_loss"
    assert trade["net_pnl"] < 0
    assert result["final_equity"] < result["starting_equity"]
    assert result["return_pct"] < 0
    assert result["ending_open_positions"] == 0  # stopped out, nothing to flatten

    # the property that makes a backtest trustworthy: an exit never lands on or
    # before the bar the position entered on.
    entry = datetime.fromisoformat(trade["entry_time"])
    exit_ = datetime.fromisoformat(trade["exit_time"])
    assert entry < exit_

    # shared-metrics shape is present (same keys the live replay prints)
    for key in ("win_rate", "profit_factor", "by_reason", "by_confidence", "fees"):
        assert key in result
    assert result["by_reason"]["stop_loss"]["count"] == 1


def test_backtest_flat_market_makes_no_trades():
    cfg = load_config()
    df = _df([(100, 101, 99, 100)] * 60)  # featureless: no OB, no structure
    result = asyncio.run(
        run_backtest(symbol="BTCUSDT", df=df, config=cfg, client=None, warmup=3)
    )

    assert result["closed_trades"] == 0
    assert result["ending_open_positions"] == 0
    assert result["final_equity"] == result["starting_equity"]
    assert result["return_pct"] == 0.0
    assert result["trades"] == []
