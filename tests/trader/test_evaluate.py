"""Walk-forward harness: folds run independently and the pooled trade count
equals the sum of the folds' trades (no leakage, no double-counting)."""
import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from backend.trader.config import load_config
from backend.trader.evaluate import format_walk_forward, walk_forward

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _wave_df(n=400, seed=7):
    """Deterministic noisy oscillation — enough swings/OBs that the rule
    policy actually trades, without depending on the network."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    base = 100 + 8 * np.sin(t / 9.0) + 4 * np.sin(t / 23.0)
    noise = rng.normal(0, 1.0, n)
    close = base + noise
    high = close + np.abs(rng.normal(0, 0.8, n)) + 0.5
    low = close - np.abs(rng.normal(0, 0.8, n)) - 0.5
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({
        "timestamp": [T0 + timedelta(hours=i) for i in range(n)],
        "open": open_, "high": high, "low": low, "close": close,
        "volume": np.full(n, 100.0),
    })


def test_walk_forward_folds_are_independent_and_pooled_is_consistent():
    cfg = load_config()
    df = _wave_df()
    report = asyncio.run(
        walk_forward(symbol="SYNTH", df=df, config=cfg, warmup=40, folds=4)
    )
    assert report["folds"] == 4
    assert report["folds_scored"] >= 1
    assert report["bars"] == len(df)

    # pooled trades must equal the sum across scored folds — proves trades
    # aren't lost or double-counted when folds run independently.
    fold_trades = sum(
        f["closed_trades"] for f in report["fold_reports"] if "closed_trades" in f
    )
    assert report["pooled_trades"] == fold_trades

    # report renders without error and carries the robustness summary
    text = format_walk_forward(report)
    assert "WALK-FORWARD" in text and "POOLED" in text
    assert 0 <= report["folds_profitable"] <= report["folds_scored"]


def test_tiny_history_skips_folds_gracefully():
    cfg = load_config()
    df = _wave_df(n=80)
    report = asyncio.run(
        walk_forward(symbol="SYNTH", df=df, config=cfg, warmup=50, folds=4)
    )
    # 80/4 = 20-bar folds, each < warmup+5 -> all skipped, nothing blows up
    assert report["folds_scored"] == 0
    assert report["pooled_trades"] == 0
    assert report["median_return_pct"] is None
