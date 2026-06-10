# %% [markdown]
# # LLM + ICT Hybrid Paper-Trading Bot — EUR/USD
#
# A EUR/USD automated **paper-trading** system that combines deterministic ICT/SMC
# detectors (context) with a **local LLM** (decision-maker) served by Ollama.
#
# **Two run modes** (set `RUN_MODE` in the config cell):
#
# 1. `"walk_forward"` — historical walk-forward backtest on an **anchored expanding
#    window** (explained inline before the backtest loop).
# 2. `"live_forward"` — streams the most recent bars in strict arrival order and
#    simulates real-time decisioning (paper trading replay; no internet at runtime).
#
# **Pipeline:** config → data load/clean/resample → ICT detectors (+ unit tests) →
# multi-timeframe context builder → LLM layer → validator → cache/log → execution
# simulator → backtest loop → metrics → HTML report + plots.
#
# Priorities, in order: **correctness, leakage-safety, reproducibility** — then returns.
#
# Everything runs locally: pandas/numpy for data, Ollama (`llama3.1:8b`) for decisions.
# A synthetic-data smoke test with a mock LLM runs first, so the notebook executes
# end-to-end even with no Parquet store and no Ollama server.

# %% [markdown]
# ## 1 · Config
#
# All tunables live here. Nothing below this cell hard-codes a knob.

# %%
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

# ----------------------------------------------------------------------------- core
ACCOUNT_EQUITY   = 100_000        # USD starting equity
RISK_PCT         = 0.01           # 1% of current equity risked per trade
RR_TARGET        = 2.0            # fixed 1:2 reward:risk
RR_TOLERANCE     = 0.15           # validator accepts RR in [2.0-tol, 2.0+tol]
SPREAD_PIPS      = 1.0            # EUR/USD spread, fixed (the only cost modelled)
PIP              = 0.0001         # EUR/USD pip size
CONF_THRESHOLD   = 70             # skip trades below this LLM confidence (gate only)
SESSION          = "new_york"     # entries only during NY session (context: all sessions)

# NY session window for *entries*, in America/New_York local time (DST-aware).
# ICT's NY forex killzone is 07:00-10:00 ET; we allow the broader NY session and
# stop before the 17:00 ET rollover. Source: innercircletrader.net killzone guide.
NY_SESSION_START = "07:00"
NY_SESSION_END   = "16:00"
TZ_NY            = ZoneInfo("America/New_York")

# ----------------------------------------------------------------------------- data
DATA_PATH        = Path("../data")            # existing Parquet store (Snappy, UTC tz-naive)
PARQUET_1M       = DATA_PATH / "eurusd_1m.parquet"
DECISION_TF      = "15min"                    # the LLM is polled once per closed bar of this TF
CONTEXT_TFS      = ["5min", "15min", "1h", "4h", "1d"]   # multi-timeframe context
FX_DAY_OFFSET    = "21h"                      # daily bars anchored 21:00 UTC (~5pm New York)

# ----------------------------------------------------------------------------- LLM
OLLAMA_MODEL     = "llama3.1:8b"
OLLAMA_URL       = "http://localhost:11434/api/generate"
OLLAMA_TIMEOUT   = 120                        # seconds per call
# Reproducibility — pinned Ollama generation params
OLLAMA_PARAMS    = {"temperature": 0, "top_p": 1, "seed": 42, "num_predict": 512}

# ----------------------------------------------------------------------------- risk circuit breakers
DAILY_LOSS_LIMIT  = 0.03    # halt new entries for the day at -3% of start-of-day equity
MAX_DRAWDOWN_HALT = 0.15    # hard-stop the whole run at -15% peak-to-trough
MAX_CONSEC_LOSSES = 5       # pause new entries for the rest of the day after N straight losses

# ----------------------------------------------------------------------------- run window
# The deterministic parts (cleaning, detectors, rule-only baseline) are cheap and can
# cover the full store. Every LLM decision is a local ~1-3 s Ollama call, so the LLM
# walk-forward window is bounded. Widen it here once a run is proven.
BACKTEST_START   = pd.Timestamp("2025-06-01")   # LLM walk-forward window start (UTC)
BACKTEST_END     = pd.Timestamp("2026-06-06")   # window end, EXCLUSIVE (store ends 2026-06-05)
WF_FOLD_FREQ     = "MS"                         # walk-forward folds: month starts
LIVE_FORWARD_DAYS = 3                           # live-forward mode replays the last N trading days

RUN_MODE         = "walk_forward"               # "walk_forward" | "live_forward"
RUN_SMOKE_ONLY   = False                        # True → skip the real-data LLM run (CI / no Ollama)
# Optional hard cap on LLM decisions for quick pipeline shakedowns (None = no cap).
MAX_LLM_DECISIONS = None

# ----------------------------------------------------------------------------- io
OUT_DIR          = Path("./runs")
CACHE_DIR        = OUT_DIR / "cache"            # LLM response cache, keyed by context hash
LOG_DIR          = OUT_DIR / "logs"             # JSONL audit log of every LLM call
REPORT_DIR       = OUT_DIR / "reports"          # HTML report, CSV trade logs, PNGs
for _d in (CACHE_DIR, LOG_DIR, REPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

RNG_SEED         = 42
np.random.seed(RNG_SEED)

print(f"config ok — decision TF {DECISION_TF}, LLM window {BACKTEST_START.date()} → {BACKTEST_END.date()}")

# %% [markdown]
# ## 2 · Data load & cleaning
#
# The canonical base is the 1-minute series. Cleaning rules:
#
# - **Weekend closure** — FX trades Sun ~17:00 → Fri ~17:00 *New York time*
#   (≈ Fri 21:00 UTC → Sun 21:00 UTC in summer, an hour later in winter). We drop the
#   closed window using New-York-local timestamps so DST is handled exactly.
# - **Holiday closures** — Christmas Day and New Year's Day (the two full FX closures)
#   are dropped by NY-local calendar date. Days with abnormally thin coverage are
#   **flagged and logged**, not dropped.
# - **Gaps** — any hole > 1 minute inside a trading session is flagged and logged.
#   We never forward-fill across a gap.

# %%
def load_1m(path: Path) -> pd.DataFrame:
    """Load the canonical 1m series → tz-naive-UTC DatetimeIndex, lowercase OHLCV."""
    df = pd.read_parquet(path)
    df = df.rename(columns={c: c.lower() for c in df.columns})
    if "datetime" in df.columns:
        df = df.set_index("datetime")
    df.index = pd.DatetimeIndex(df.index)
    if df.index.tz is not None:                       # store is tz-naive UTC by contract
        df.index = df.index.tz_convert("UTC").tz_localize(None)
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def _ny_local(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Interpret a tz-naive-UTC index in New York local time (DST-aware)."""
    return index.tz_localize("UTC").tz_convert(TZ_NY)


def drop_weekends(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop bars inside the weekly FX closure: Fri 17:00 → Sun 17:00 New York time."""
    ny = _ny_local(df.index)
    dow, minutes = ny.dayofweek, ny.hour * 60 + ny.minute
    closed = ((dow == 4) & (minutes >= 17 * 60)) | (dow == 5) | ((dow == 6) & (minutes < 17 * 60))
    return df[~closed], int(closed.sum())


def drop_holidays(df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """Drop the two full FX closures (Dec 25, Jan 1) by NY-local date."""
    ny = _ny_local(df.index)
    holiday = ((ny.month == 12) & (ny.day == 25)) | ((ny.month == 1) & (ny.day == 1))
    dropped_days = sorted({d.date() for d in ny[holiday]})
    return df[~holiday], dropped_days


def flag_gaps(df: pd.DataFrame, min_gap: str = "2min") -> pd.DataFrame:
    """Log every intra-session hole in the 1m series (> 1 missing minute).

    The weekly closure (Fri→Sun NY) is expected and excluded. Gaps are *flagged*,
    never filled — the resampler simply aggregates the bars that exist.
    """
    diffs = df.index.to_series().diff()
    holes = diffs[diffs >= pd.Timedelta(min_gap)]
    records = []
    for end_ts, delta in holes.items():
        start_ts = end_ts - delta
        ny = start_ts.tz_localize("UTC").tz_convert(TZ_NY)
        is_weekend = ny.dayofweek == 4 and ny.hour >= 16   # gap starting Fri afternoon NY
        if delta >= pd.Timedelta("1D") and is_weekend:
            continue
        records.append({"gap_start": start_ts, "gap_end": end_ts,
                        "minutes": delta.total_seconds() / 60})
    return pd.DataFrame(records)


def flag_thin_days(df: pd.DataFrame, min_frac: float = 0.5) -> pd.DataFrame:
    """Flag trading days with < min_frac of the median per-day bar count (logged only)."""
    counts = df.groupby(df.index.normalize()).size()
    median = counts.median()
    thin = counts[counts < min_frac * median]
    return thin.rename("bars").to_frame().assign(median_bars=median)
