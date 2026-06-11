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
# The decision layer is provider-pluggable: local Ollama (`llama3.1:8b`) honors the
# original local-only spec; the Gemini API is available as a **documented temporary
# exception** for speed/reasoning on memory-constrained hardware (see config + §9).
# A synthetic-data smoke test with a mock LLM runs first, so the notebook executes
# end-to-end even with no Parquet store and no LLM provider at all.

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
import shutil
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
RR_TARGET        = float(os.environ.get("BOT_RR", "2.0"))   # reward:risk (BOT_RR-overridable)
RR_TOLERANCE     = 0.15           # validator accepts RR in [target-tol, target+tol]
RR_FREE          = bool(os.environ.get("BOT_RR_FREE"))      # variable-RR (liquidity targets)
SPREAD_PIPS      = 1.0            # EUR/USD spread, fixed (the only cost modelled)
PIP              = 0.0001         # EUR/USD pip size
CONF_THRESHOLD   = 70             # skip trades below this LLM confidence (gate only)
SESSION          = "new_york"     # entries only during NY session (context: all sessions)

# NY session window for *entries*, in America/New_York local time (DST-aware).
# ICT's NY forex killzone is 07:00-10:00 ET; we allow the broader NY session and
# stop before the 17:00 ET rollover. Source: innercircletrader.net killzone guide.
# Env-overridable so the multi-strategy compare run can widen the decision window to
# 00:00-16:00 ET (MIDNIGHT RAID needs 00:00-05:00); each strategy self-gates internally.
NY_SESSION_START = os.environ.get("BOT_SESSION_START", "07:00")
NY_SESSION_END   = os.environ.get("BOT_SESSION_END", "16:00")
TZ_NY            = ZoneInfo("America/New_York")

# ----------------------------------------------------------------------------- data
DATA_PATH        = Path("../data")            # existing Parquet store (Snappy, UTC tz-naive)
PARQUET_1M       = DATA_PATH / "eurusd_1m.parquet"
DECISION_TF      = "15min"                    # the LLM is polled once per closed bar of this TF
CONTEXT_TFS      = ["5min", "15min", "1h", "4h", "1d"]   # multi-timeframe context
FX_DAY_OFFSET    = "21h"                      # daily bars anchored 21:00 UTC (~5pm New York)

# ----------------------------------------------------------------------------- LLM
# Provider is pluggable. "ollama" honors the original spec (local LLM, no internet,
# no paid APIs). "gemini" is a DOCUMENTED TEMPORARY EXCEPTION to that spec, adopted
# because this 16GB machine sustains only ~35-45s per llama3.1:8b call (memory
# pressure), making long windows impractical; reasoning quality was the priority.
# BOT_* env vars override per process, so parallel runs (e.g. a second Ollama box on
# the LAN serving a different model) need no file edits — just a copied notebook.
LLM_PROVIDER     = os.environ.get("BOT_LLM_PROVIDER", "ollama")   # "gemini" | "ollama"

OLLAMA_MODEL     = os.environ.get("BOT_OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_URL       = os.environ.get("BOT_OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_TIMEOUT   = 300                        # seconds per call (LAN box can be slow)
# Reproducibility — pinned Ollama generation params
OLLAMA_PARAMS    = {"temperature": 0, "top_p": 1, "seed": 42, "num_predict": 512}

GEMINI_MODEL     = "gemini-2.5-flash-lite"
# Key sources, in order: env var, then a git-ignored `.gemini_key` file next to the
# notebook (so the key never has to appear in shell history or the repo).
GEMINI_API_KEY   = os.environ.get("GEMINI_API_KEY", "") \
    or (Path(".gemini_key").read_text().strip() if Path(".gemini_key").exists() else "")
GEMINI_URL       = ("https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{GEMINI_MODEL}:generateContent")
GEMINI_TIMEOUT   = 60
# Client-side pacing: free tier allows ~15 req/min — spacing calls beats bouncing off
# 429s (retries there are capped and an exhausted call is skipped). 0 on a paid key.
GEMINI_MIN_INTERVAL_S = 4.1
# Pinned Gemini generation params. Note: Gemini accepts a seed but does not guarantee
# bit-identical replays across backend versions — the context-hash cache is what makes
# this notebook reproducible end-to-end regardless of provider.
GEMINI_PARAMS    = {"temperature": 0, "topP": 1, "seed": 42, "maxOutputTokens": 1024}

LLM_MODEL_ID     = f"{LLM_PROVIDER}:{GEMINI_MODEL if LLM_PROVIDER == 'gemini' else OLLAMA_MODEL}"

# ----------------------------------------------------------------------------- risk circuit breakers
# BOT_NO_BREAKERS=1 lifts the halts (limits → effectively infinite) to expose the raw
# signal trajectory over a long window — the spec'd defaults otherwise hard-stop the
# whole run at -15% peak-to-trough, which on a losing strategy ends trading early.
_NO_BRK = bool(os.environ.get("BOT_NO_BREAKERS"))
DAILY_LOSS_LIMIT  = 99.0 if _NO_BRK else 0.03   # halt new entries for the day at -3% SOD equity
MAX_DRAWDOWN_HALT = 99.0 if _NO_BRK else 0.15   # hard-stop the whole run at -15% peak-to-trough
MAX_CONSEC_LOSSES = 10**9 if _NO_BRK else 5     # pause entries for the day after N straight losses

# ----------------------------------------------------------------------------- run window
# The deterministic parts (cleaning, detectors, rule-only baseline) are cheap and can
# cover the full store. The LLM walk-forward window is bounded by per-call latency:
# measured ~35-45s/call for local llama3.1:8b on this 16GB machine vs ~1-2s for the
# Gemini API. Widen the window freely — the context-hash cache replays completed calls.
# Window is BOT_START/BOT_END env-overridable for one-off runs without editing the
# notebook. Defaults: the February local-LLM window. START stays anchored across
# widenings so completed contexts hash identically and replay from cache.
# (END is EXCLUSIVE; the store ends 2026-06-05.)
BACKTEST_START   = pd.Timestamp(os.environ.get("BOT_START", "2026-02-01"))
BACKTEST_END     = pd.Timestamp(os.environ.get("BOT_END", "2026-03-01"))
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
# %% [markdown]
# ## 3 · Resampling — closed candles only
#
# All higher timeframes are derived from the cleaned 1m series with pandas
# `resample()` (open=first, high=max, low=min, close=last, volume=sum).
#
# **The `close_time` convention (load-bearing for leakage safety):** every resampled
# bar carries `close_time = bar_start + timeframe`. A bar is *knowable* at decision
# time `t` iff `close_time <= t`. The context builder filters on exactly that, so a
# partially-formed current bar can never appear in any context.
#
# Daily bars are anchored at **21:00 UTC** (≈ 5 pm New York, the FX day boundary), so
# Sunday-evening trading folds into Monday's daily bar instead of forming stub bars.
# The anchor is fixed year-round; in winter the true 5 pm-ET boundary is 22:00 UTC, so
# daily bars are offset by 1 h for part of the year — a documented simplification that
# affects only daily-bar aesthetics, never intraday logic.

# %%
TF_DELTA = {
    "1min": pd.Timedelta("1min"), "5min": pd.Timedelta("5min"),
    "15min": pd.Timedelta("15min"), "1h": pd.Timedelta("1h"),
    "4h": pd.Timedelta("4h"), "1d": pd.Timedelta("24h"),
}

def resample_ohlcv(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample cleaned 1m bars to `rule`. Bars are labelled by start time and carry
    an explicit `close_time`; bars with no underlying 1m data are dropped (gaps are
    never filled)."""
    if rule == "1min":
        out = df_1m.copy()
    else:
        resampler = (df_1m.resample("24h", offset=FX_DAY_OFFSET) if rule == "1d"
                     else df_1m.resample(rule))
        out = resampler.agg(open=("open", "first"), high=("high", "max"),
                            low=("low", "min"), close=("close", "last"),
                            volume=("volume", "sum")).dropna(subset=["open"])
    out["close_time"] = out.index + TF_DELTA[rule]
    return out


def build_tf_store(df_1m: pd.DataFrame, tfs: Iterable[str]) -> dict[str, pd.DataFrame]:
    """Resample the cleaned 1m base once per context timeframe."""
    return {tf: resample_ohlcv(df_1m, tf) for tf in tfs}

# %% [markdown]
# ## 4 · Synthetic fixture data
#
# A deterministic synthetic 1m EUR/USD series (seeded random walk over weekdays).
# It powers the detector unit tests' property checks and the end-to-end smoke test,
# so the notebook runs even before the real Parquet store is wired in.

# %%
def make_synthetic_1m(start: str = "2024-01-01", days: int = 20,
                      seed: int = RNG_SEED, p0: float = 1.0850) -> pd.DataFrame:
    """Seeded 1m random-walk OHLCV over `days` weekdays, 24h/day, tz-naive UTC."""
    rng = np.random.default_rng(seed)
    bdays = pd.bdate_range(start, periods=days)
    idx = pd.DatetimeIndex(
        np.concatenate([pd.date_range(d, periods=1440, freq="1min").values for d in bdays])
    )
    n = len(idx)
    # mid-price walk with mild vol clustering
    vol = 0.00006 * (1 + 0.5 * np.sin(np.arange(n) / 700))
    steps = rng.normal(0, 1, n) * vol
    mid = p0 + np.cumsum(steps)
    opens = np.r_[p0, mid[:-1]]
    closes = mid
    wick = np.abs(rng.normal(0, 1, (2, n))) * vol
    highs = np.maximum(opens, closes) + wick[0]
    lows = np.minimum(opens, closes) - wick[1]
    volume = rng.lognormal(3, 1, n).round(2)
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": volume}, index=idx)
    return df.round(5)

print("synthetic generator ready:", make_synthetic_1m(days=2).shape)

# %% [markdown]
# ## 5 · Load & clean the real store
#
# Loads the canonical 1m Parquet if present, otherwise falls back to synthetic data
# (`HAVE_REAL_DATA` records which). Cleaning results — weekend bars dropped, holidays
# dropped, gaps and thin days flagged — are printed and kept for the report.

# %%
HAVE_REAL_DATA = PARQUET_1M.exists()

raw_1m = load_1m(PARQUET_1M) if HAVE_REAL_DATA else make_synthetic_1m(days=40)
clean_1m, n_weekend = drop_weekends(raw_1m)
clean_1m, holiday_days = drop_holidays(clean_1m)
gaps_df = flag_gaps(clean_1m)
thin_days = flag_thin_days(clean_1m)

print(f"source            : {'real parquet' if HAVE_REAL_DATA else 'SYNTHETIC fallback'}")
print(f"raw 1m bars       : {len(raw_1m):,}")
print(f"weekend bars cut  : {n_weekend:,}")
print(f"holiday days cut  : {len(holiday_days)}")
print(f"clean 1m bars     : {len(clean_1m):,}  ({clean_1m.index[0]} → {clean_1m.index[-1]})")
print(f"intra-session gaps: {len(gaps_df):,} flagged (largest: "
      f"{gaps_df['minutes'].max():.0f} min)" if len(gaps_df) else "intra-session gaps: none")
print(f"thin days flagged : {len(thin_days)} (logged, not dropped)")

TF_STORE = build_tf_store(clean_1m, CONTEXT_TFS)
for tf, d in TF_STORE.items():
    print(f"  {tf:>5}: {len(d):,} bars")
# %% [markdown]
# ## 6 · ICT/SMC detectors
#
# Deterministic, pure functions over an OHLCV DataFrame. They produce **context**
# records for the LLM, not trade signals. Definitions were researched fresh; each
# detector's docstring states the definition implemented and its source.
#
# | Concept | Definition implemented | Primary source |
# |---|---|---|
# | Fair Value Gap | 3-candle imbalance: bullish when `low[i] > high[i-2]` (gap = that span); candle 2 is the displacement candle | [innercircletrader.net — Valid ICT FVG](https://innercircletrader.net/tutorials/valid-ict-fair-value-gap/), [TrendSpider](https://trendspider.com/learning-center/fair-value-gap-trading-strategy/) |
# | Order Block | Last opposing candle before a displacement move that creates an FVG | [TradeZella — Key ICT Concepts](https://www.tradezella.com/learning-items/key-ict-concepts), [ePlanet](https://eplanetbrokers.com/training/what-is-fair-value-gap) |
# | Liquidity Sweep | Wick trades through a prior swing high/low, candle closes back inside | [Zeiierman](https://www.zeiierman.com/blog/liquidity-sweeps-in-trading), [Aron Groups — Liquidity in ICT](https://arongroups.co/technical-analyze/liquidity-in-ict/) |
# | BOS / CHoCH / MSS | BOS: close beyond a swing *with* the trend (continuation); CHoCH: close beyond a counter-trend swing (first reversal warning); HH/HL/LH/LL tracked | [innercircletrader.net — MSS](https://innercircletrader.net/tutorials/ict-market-structure-shift/), [FXOpen](https://fxopen.com/blog/en/market-structure-shift-meaning-and-use-in-ict-trading/), [TSG — BOS & CHoCH](https://tradingstrategyguides.com/day-3-smc-ict-market-structure-explained-bos-choch-swing-points-2026/) |
# | Liquidity Pool (EQH/EQL) | Consecutive swing highs/lows within a small tolerance — resting stop clusters | [Aron Groups](https://arongroups.co/technical-analyze/liquidity-in-ict/), [TradingFinder — Dealing Range](https://tradingfinder.com/education/forex/ict-dealing-range/) |
# | Premium / Discount | Equilibrium = 50% of the dealing range (swing low ↔ swing high); above = premium, below = discount | [TheSimpleICT](https://thesimpleict.com/premium-vs-discount-ict-smart-money/), [ICTFlow](https://ictflow.com/blog/ict-premium-discount-zones) |
# | Displacement | High-momentum candle: body ≫ recent average body — institutional participation | [TheSimpleICT — Dealing Range](https://thesimpleict.com/dealing-range-ict-guide/), [FXNX](https://fxnx.com/en/blog/ict-dealing-range-map-institutional-moves) |
#
# **Point-in-time discipline:** every record carries `confirmed_time` — the bar close
# at which the event became knowable. A swing with pivot strength `k` is only knowable
# `k` bars after its extreme prints; an FVG only when its third candle closes. The
# context builder filters on `confirmed_time <= decision_time`, and a prefix-consistency
# property test below proves no detector ever looks ahead.

# %%
# --- detector tunables -------------------------------------------------------------
SWING_K             = 2      # pivot strength: bars on each side that must be exceeded
DISPLACEMENT_FACTOR = 2.0    # candle body > factor × rolling mean body
DISPLACEMENT_WINDOW = 20     # rolling window for the mean body
EQ_TOL_PIPS         = 2.0    # equal-highs/lows tolerance
SWEEP_LOOKBACK_BARS = 500    # how long a swing level stays sweepable
OB_SCAN_BACK        = 10     # bars to scan back for the last opposing candle
SCAN_CAP_BARS       = 20_000 # forward-scan cap for FVG fill times


def _first_idx_after(arr: np.ndarray, start: int, threshold: float, op: str,
                     cap: int = SCAN_CAP_BARS) -> int:
    """First index j > start where `arr[j] <op> threshold`, scanning in chunks.
    Returns -1 if not found within `cap` bars. Pure index arithmetic — no pandas."""
    n = len(arr)
    j = start + 1
    end = min(n, start + 1 + cap)
    while j < end:
        hi = min(j + 256, end)
        chunk = arr[j:hi]
        mask = (chunk <= threshold) if op == "le" else (chunk >= threshold) \
            if op == "ge" else (chunk < threshold) if op == "lt" else (chunk > threshold)
        hit = int(np.argmax(mask))
        if mask[hit]:
            return j + hit
        j = hi
    return -1


def detect_swings(df: pd.DataFrame, k: int = SWING_K) -> pd.DataFrame:
    """Swing (fractal) pivots: a swing high at bar i has a high strictly greater than
    the highs of the k bars on each side; mirror for swing lows. The pivot is only
    *knowable* once the k-th later bar closes → `confirmed_time = close_time[i+k]`.
    Swings are the substrate for sweeps, structure, pools and the dealing range.
    (Standard SMC swing-point definition — see TSG Day-3 market-structure guide.)"""
    h, l = df["high"].values, df["low"].values
    n = len(df)
    sh = np.ones(n, bool)
    sl = np.ones(n, bool)
    for j in range(1, k + 1):
        sh[:j], sh[n - j:] = False, False
        sl[:j], sl[n - j:] = False, False
        sh[j:n] &= h[j:n] > h[:n - j]          # vs j bars before
        sh[:n - j] &= h[:n - j] > h[j:n]       # vs j bars after
        sl[j:n] &= l[j:n] < l[:n - j]
        sl[:n - j] &= l[:n - j] < l[j:n]
    rows = []
    ct = df["close_time"].values
    idx = df.index
    for i in np.flatnonzero(sh | sl):
        if i + k >= n:
            continue                            # not yet confirmable inside this df
        if sh[i]:
            rows.append({"time": idx[i], "kind": "high", "level": h[i],
                         "confirmed_time": ct[i + k]})
        if sl[i]:
            rows.append({"time": idx[i], "kind": "low", "level": l[i],
                         "confirmed_time": ct[i + k]})
    cols = ["time", "kind", "level", "confirmed_time"]
    out = pd.DataFrame(rows, columns=cols).sort_values(
        ["confirmed_time", "time", "kind"], kind="mergesort")   # stable: ties deterministic
    return out.reset_index(drop=True)


def detect_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """Fair Value Gaps — the ICT 3-candle imbalance.
    Bullish FVG: `low[i] > high[i-2]` → untraded span [high[i-2], low[i]]; candle i-1
    is the displacement candle. Bearish mirror: `high[i] < low[i-2]`.
    Knowable when candle i closes → `confirmed_time = close_time[i]`.
    Sources: innercircletrader.net 'Valid ICT Fair Value Gap', TrendSpider FVG guide.

    `touch_time`/`fill_time` (first re-entry into the gap / first full fill) are
    computed over the full series ONCE for speed. They are leakage-safe because the
    context builder only ever *compares* them against the decision time t — i.e. it
    reveals exactly 'is this gap still open as of t?', nothing about the future."""
    h, l = df["high"].values, df["low"].values
    n = len(df)
    ct, idx = df["close_time"].values, df.index
    rows = []
    bull = np.flatnonzero(l[2:] > h[:-2]) + 2
    bear = np.flatnonzero(h[2:] < l[:-2]) + 2
    for i in bull:
        bottom, top = h[i - 2], l[i]
        ft = _first_idx_after(l, i, top, "lt")     # price re-enters gap from above
        ff = _first_idx_after(l, i, bottom, "le")  # full fill: traded to far edge
        rows.append({"time": idx[i], "direction": "bullish", "top": top, "bottom": bottom,
                     "confirmed_time": ct[i],
                     "touch_time": ct[ft] if ft >= 0 else pd.NaT,    # close-time of the
                     "fill_time": ct[ff] if ff >= 0 else pd.NaT})    # touching/filling bar
    for i in bear:
        bottom, top = h[i], l[i - 2]
        ft = _first_idx_after(h, i, bottom, "gt")
        ff = _first_idx_after(h, i, top, "ge")
        rows.append({"time": idx[i], "direction": "bearish", "top": top, "bottom": bottom,
                     "confirmed_time": ct[i],
                     "touch_time": ct[ft] if ft >= 0 else pd.NaT,
                     "fill_time": ct[ff] if ff >= 0 else pd.NaT})
    cols = ["time", "direction", "top", "bottom", "confirmed_time", "touch_time", "fill_time"]
    return (pd.DataFrame(rows, columns=cols)
            .sort_values(["time", "direction"], kind="mergesort").reset_index(drop=True))


def detect_displacement(df: pd.DataFrame, factor: float = DISPLACEMENT_FACTOR,
                        window: int = DISPLACEMENT_WINDOW) -> pd.DataFrame:
    """Displacement — a high-momentum, large-bodied candle signalling institutional
    participation (TheSimpleICT / FXNX dealing-range guides). Implemented as:
    candle body > `factor` × rolling mean body of the prior `window` candles.
    Knowable at its own close."""
    body = (df["close"] - df["open"]).abs()
    avg = body.rolling(window, min_periods=5).mean().shift(1)
    mask = (body > factor * avg).to_numpy()
    out = pd.DataFrame({
        "time": df.index[mask],
        "direction": np.where(df["close"].to_numpy()[mask] >= df["open"].to_numpy()[mask],
                              "bullish", "bearish"),
        "body": body.to_numpy()[mask],
        "confirmed_time": df["close_time"].to_numpy()[mask],
    })
    return out.reset_index(drop=True)


def detect_order_blocks(df: pd.DataFrame, fvg: Optional[pd.DataFrame] = None,
                        displacement: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Order Blocks — 'the last opposing candle before a displacement move; the base
    of an impulsive move where large orders entered' (TradeZella key-ICT-concepts;
    ePlanet OB guide). Implemented strictly: a bullish OB is the last down-close
    candle within `OB_SCAN_BACK` bars before an up-displacement candle that creates
    a bullish FVG (the FVG requirement is the imbalance confirmation the sources
    demand). Zone = the opposing candle's full range. Knowable when the FVG's third
    candle closes (that is when the displacement is proven)."""
    fvg = detect_fvg(df) if fvg is None else fvg
    disp = detect_displacement(df) if displacement is None else displacement
    disp_times = set(disp["time"])
    o, c, h, l = df["open"].values, df["close"].values, df["high"].values, df["low"].values
    pos = {t: i for i, t in enumerate(df.index)}
    rows, seen = [], set()
    for r in fvg.itertuples():
        i = pos[r.time]                      # third candle of the FVG
        mid = i - 1                          # displacement candle
        if mid < 0 or df.index[mid] not in disp_times:
            continue
        want_down = r.direction == "bullish"   # bullish OB = last down-close candle
        for j in range(mid - 1, max(-1, mid - 1 - OB_SCAN_BACK), -1):
            opposing = (c[j] < o[j]) if want_down else (c[j] > o[j])
            if opposing:
                if df.index[j] in seen:
                    break
                seen.add(df.index[j])
                rows.append({"time": df.index[j],
                             "direction": "bullish" if want_down else "bearish",
                             "top": h[j], "bottom": l[j],
                             "confirmed_time": r.confirmed_time})
                break
    cols = ["time", "direction", "top", "bottom", "confirmed_time"]
    return (pd.DataFrame(rows, columns=cols)
            .sort_values(["time", "direction"], kind="mergesort").reset_index(drop=True))


def detect_liquidity_sweeps(df: pd.DataFrame, swings: Optional[pd.DataFrame] = None,
                            lookback: int = SWEEP_LOOKBACK_BARS) -> pd.DataFrame:
    """Liquidity Sweeps — price wicks through a prior swing high/low (where resting
    stops sit), then the candle closes back inside the range: a stop raid, not a
    genuine break (Zeiierman liquidity-sweep guide; Aron Groups 'Liquidity in ICT').
    Sweeping a swing HIGH grabs buy-side liquidity (bearish implication); sweeping a
    swing LOW grabs sell-side liquidity (bullish implication).
    For each confirmed swing, the FIRST later candle to trade through the level
    decides: close back inside → sweep; close beyond → genuine break, no record.
    Knowable at that candle's close."""
    swings = detect_swings(df) if swings is None else swings
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    ct, idx = df["close_time"].values, df.index
    pos = {t: i for i, t in enumerate(idx)}
    rows = []
    for s in swings.itertuples():
        i = pos[s.time]
        start = i + SWING_K                  # search begins after the pivot confirms
        if s.kind == "high":
            j = _first_idx_after(h, start, s.level, "gt", cap=lookback)
            if j >= 0 and c[j] < s.level:
                rows.append({"time": idx[j], "side": "buyside", "swept_level": s.level,
                             "swing_time": s.time, "wick_extreme": h[j],
                             "confirmed_time": ct[j]})
        else:
            j = _first_idx_after(l, start, s.level, "lt", cap=lookback)
            if j >= 0 and c[j] > s.level:
                rows.append({"time": idx[j], "side": "sellside", "swept_level": s.level,
                             "swing_time": s.time, "wick_extreme": l[j],
                             "confirmed_time": ct[j]})
    cols = ["time", "side", "swept_level", "swing_time", "wick_extreme", "confirmed_time"]
    out = pd.DataFrame(rows, columns=cols).sort_values(
        ["time", "swing_time", "swept_level"], kind="mergesort")  # one candle can sweep
    return out.reset_index(drop=True)                             # several levels at once


def detect_structure(df: pd.DataFrame, swings: Optional[pd.DataFrame] = None
                     ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Market structure — BOS / CHoCH events + HH/HL/LH/LL swing labels.
    BOS (Break of Structure): a candle CLOSE beyond the latest confirmed swing in the
    direction of the prevailing trend → continuation. CHoCH (Change of Character): a
    close beyond the latest counter-trend swing → first reversal warning. The first
    structural break of a run (no trend yet) is labelled CHoCH, matching the
    'change of character starts a new regime' reading.
    Sources: innercircletrader.net MSS guide; FXOpen MSS; TSG Day-3 (BOS continuation
    vs CHoCH reversal; swing labels HH/HL/LH/LL).
    Returns (events, labelled_swings); events are knowable at the breaking close."""
    swings = detect_swings(df) if swings is None else swings
    c = df["close"].values
    ct, idx = df["close_time"].values, df.index
    pos = {t: i for i, t in enumerate(idx)}
    confirm_at: dict[int, list] = {}
    for s in swings.itertuples():
        confirm_at.setdefault(pos[s.time] + SWING_K, []).append(s)
    trend = 0                                # 0 none, +1 up, -1 down
    ref_high = ref_low = None                # latest confirmed, not-yet-broken swings
    last_high_lvl = last_low_lvl = None
    labels, events = [], []
    for i in range(len(df)):
        for s in confirm_at.get(i, []):
            if s.kind == "high":
                lab = None if last_high_lvl is None else ("HH" if s.level > last_high_lvl else "LH")
                last_high_lvl, ref_high = s.level, s
            else:
                lab = None if last_low_lvl is None else ("HL" if s.level > last_low_lvl else "LL")
                last_low_lvl, ref_low = s.level, s
            labels.append({"time": s.time, "kind": s.kind, "level": s.level,
                           "label": lab, "confirmed_time": s.confirmed_time})
        if ref_high is not None and c[i] > ref_high.level:
            kind = "BOS_up" if trend == 1 else "CHoCH_up"
            trend = 1
            events.append({"time": idx[i], "kind": kind, "level": ref_high.level,
                           "trend_after": trend, "confirmed_time": ct[i]})
            ref_high = None
        elif ref_low is not None and c[i] < ref_low.level:
            kind = "BOS_down" if trend == -1 else "CHoCH_down"
            trend = -1
            events.append({"time": idx[i], "kind": kind, "level": ref_low.level,
                           "trend_after": trend, "confirmed_time": ct[i]})
            ref_low = None
    ev_cols = ["time", "kind", "level", "trend_after", "confirmed_time"]
    lb_cols = ["time", "kind", "level", "label", "confirmed_time"]
    return (pd.DataFrame(events, columns=ev_cols),
            pd.DataFrame(labels, columns=lb_cols))


def detect_equal_levels(df: pd.DataFrame, swings: Optional[pd.DataFrame] = None,
                        tol_pips: float = EQ_TOL_PIPS) -> pd.DataFrame:
    """Liquidity Pools — equal highs (EQH) / equal lows (EQL): consecutive swing
    highs/lows within `tol_pips`, marking clustered resting stops (buy-side liquidity
    above EQH, sell-side below EQL). Sources: Aron Groups 'Liquidity in ICT';
    TradingFinder dealing-range liquidity guide.
    Knowable when the second swing of the pair confirms."""
    swings = detect_swings(df) if swings is None else swings
    rows = []
    for kind, pool, side in (("high", "EQH", "buyside"), ("low", "EQL", "sellside")):
        sub = swings[swings["kind"] == kind].sort_values("time")
        prev = None
        for s in sub.itertuples():
            if prev is not None and abs(s.level - prev.level) <= tol_pips * PIP:
                rows.append({"time": s.time, "pool": pool, "side": side,
                             "level": max(s.level, prev.level) if kind == "high"
                                      else min(s.level, prev.level),
                             "first_time": prev.time, "confirmed_time": s.confirmed_time})
            prev = s
    cols = ["time", "pool", "side", "level", "first_time", "confirmed_time"]
    return (pd.DataFrame(rows, columns=cols)
            .sort_values(["time", "pool", "level"], kind="mergesort").reset_index(drop=True))


def premium_discount(visible_swings: pd.DataFrame, price: float,
                     eq_band: float = 0.05) -> Optional[dict]:
    """Premium / Discount — the dealing range runs from the most recent confirmed
    swing low to the most recent confirmed swing high; equilibrium is its 50% level.
    Price above EQ trades at a premium (favour shorts / take profits), below EQ at a
    discount (favour longs). Sources: TheSimpleICT premium-vs-discount; ICTFlow
    premium-discount zones. Pure function of already-visible swings → inherently
    point-in-time."""
    highs = visible_swings[visible_swings["kind"] == "high"]
    lows = visible_swings[visible_swings["kind"] == "low"]
    if highs.empty or lows.empty:
        return None
    hi = highs.iloc[-1]["level"]
    lo = lows.iloc[-1]["level"]
    if hi <= lo:
        return None
    pos = (price - lo) / (hi - lo)
    zone = "premium" if pos > 0.5 + eq_band else "discount" if pos < 0.5 - eq_band else "equilibrium"
    return {"range_high": hi, "range_low": lo, "equilibrium": (hi + lo) / 2,
            "position": pos, "zone": zone}


def run_all_detectors(df: pd.DataFrame) -> dict:
    """Run every detector once over a (full-history) TF frame. The context builder
    later filters each record set by `confirmed_time <= decision_time`."""
    swings = detect_swings(df)
    fvg = detect_fvg(df)
    disp = detect_displacement(df)
    events, labelled = detect_structure(df, swings)
    return {"swings": labelled, "fvg": fvg, "displacement": disp,
            "order_blocks": detect_order_blocks(df, fvg, disp),
            "sweeps": detect_liquidity_sweeps(df, swings),
            "structure": events,
            "pools": detect_equal_levels(df, swings)}

print("detectors defined")
# %% [markdown]
# ## 7 · Detector unit tests
#
# Two layers:
#
# 1. **Hand-crafted fixtures** — tiny bar sequences where the expected detections are
#    known exactly (levels, directions, confirmation times).
# 2. **Prefix-consistency property test** — for any cut point `t`, running a detector
#    on `df[:t]` must yield *exactly* the records the full run produces with
#    `confirmed_time <= t`. If a detector peeked at future bars, prefix and full runs
#    would disagree. This is the machine-checked no-lookahead guarantee.

# %%
def bars(rows, start="2024-01-06", freq="15min") -> pd.DataFrame:
    """Fixture builder: rows of (open, high, low, close) → OHLCV frame w/ close_time."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    df["close_time"] = df.index + pd.Timedelta(freq)
    return df


# --- FVG: bullish gap, touch, full fill -------------------------------------------
fx = bars([
    (1.0000, 1.0010, 0.9990, 1.0005),   # c0
    (1.0005, 1.0040, 1.0004, 1.0038),   # c1 displacement
    (1.0038, 1.0050, 1.0020, 1.0045),   # c2 → bullish FVG [1.0010, 1.0020]
    (1.0045, 1.0048, 1.0015, 1.0046),   # c3 dips into gap (touch, not full fill)
    (1.0046, 1.0047, 1.0005, 1.0040),   # c4 trades to far edge (full fill)
])
f = detect_fvg(fx)
assert len(f) == 1 and f.iloc[0]["direction"] == "bullish"
assert math.isclose(f.iloc[0]["bottom"], 1.0010) and math.isclose(f.iloc[0]["top"], 1.0020)
assert f.iloc[0]["confirmed_time"] == fx["close_time"].iloc[2]
assert f.iloc[0]["touch_time"] == fx["close_time"].iloc[3]   # knowable at that bar's close
assert f.iloc[0]["fill_time"] == fx["close_time"].iloc[4]

# bearish mirror
fx2 = bars([
    (1.0050, 1.0060, 1.0040, 1.0045),
    (1.0045, 1.0046, 1.0010, 1.0012),
    (1.0012, 1.0030, 1.0005, 1.0008),   # high 1.0030 < low c0 1.0040 → bearish FVG
])
f2 = detect_fvg(fx2)
assert len(f2) == 1 and f2.iloc[0]["direction"] == "bearish"
assert math.isclose(f2.iloc[0]["bottom"], 1.0030) and math.isclose(f2.iloc[0]["top"], 1.0040)

# --- swings: pivot strength & confirmation lag -------------------------------------
sx = bars([
    (1.0010, 1.0015, 1.0005, 1.0012),
    (1.0012, 1.0020, 1.0008, 1.0018),
    (1.0018, 1.0050, 1.0016, 1.0045),   # swing high @2 (k=2)
    (1.0045, 1.0046, 1.0014, 1.0020),
    (1.0020, 1.0025, 1.0012, 1.0022),
    (1.0022, 1.0028, 1.0015, 1.0024),
])
sw = detect_swings(sx)
sw_h = sw[sw["kind"] == "high"]
assert len(sw_h) == 1 and math.isclose(sw_h.iloc[0]["level"], 1.0050)
assert sw_h.iloc[0]["time"] == sx.index[2]
assert sw_h.iloc[0]["confirmed_time"] == sx["close_time"].iloc[4]   # knowable k bars later

# --- liquidity sweep: wick through swing high, close back inside -------------------
lx = bars([
    (1.0010, 1.0018, 1.0005, 1.0012),
    (1.0012, 1.0020, 1.0008, 1.0015),
    (1.0015, 1.0030, 1.0012, 1.0022),   # swing high @2, level 1.0030
    (1.0022, 1.0024, 1.0010, 1.0014),
    (1.0014, 1.0022, 1.0008, 1.0016),   # swing confirmed at close of @4
    (1.0016, 1.0035, 1.0012, 1.0020),   # wick 1.0035 > 1.0030, close 1.0020 < 1.0030 → sweep
])
sweeps = detect_liquidity_sweeps(lx)
assert len(sweeps) == 1
s0 = sweeps.iloc[0]
assert s0["side"] == "buyside" and math.isclose(s0["swept_level"], 1.0030)
assert s0["time"] == lx.index[5] and math.isclose(s0["wick_extreme"], 1.0035)

# negative control: a close *above* the level is a genuine break, not a sweep
lx_break = lx.copy()
lx_break.loc[lx_break.index[5], "close"] = 1.0032   # still under the 1.0035 wick high
assert len(detect_liquidity_sweeps(lx_break)) == 0

# --- market structure: CHoCH → HH/HL labels → BOS ----------------------------------
mx = bars([
    (1.0020, 1.0030, 1.0010, 1.0022),
    (1.0022, 1.0028, 1.0008, 1.0018),
    (1.0018, 1.0020, 1.0000, 1.0012),   # swing low @2 (1.0000)
    (1.0012, 1.0022, 1.0005, 1.0018),
    (1.0018, 1.0024, 1.0006, 1.0020),
    (1.0020, 1.0040, 1.0015, 1.0030),   # swing high @5 (1.0040)
    (1.0030, 1.0030, 1.0012, 1.0016),
    (1.0016, 1.0028, 1.0010, 1.0015),   # swing low @7 (1.0010) → HL
    (1.0015, 1.0045, 1.0013, 1.0044),   # close 1.0044 > 1.0040 → CHoCH_up (first break)
    (1.0044, 1.0050, 1.0016, 1.0042),   # swing high @9 (1.0050) → HH
    (1.0038, 1.0040, 1.0020, 1.0030),
    (1.0030, 1.0038, 1.0018, 1.0028),
    (1.0028, 1.0060, 1.0025, 1.0058),   # close 1.0058 > 1.0050 → BOS_up (trend continues)
    (1.0050, 1.0055, 1.0030, 1.0040),
    (1.0040, 1.0050, 1.0032, 1.0042),
])
events, labelled = detect_structure(mx)
assert list(events["kind"]) == ["CHoCH_up", "BOS_up"], list(events["kind"])
assert events.iloc[0]["time"] == mx.index[8] and math.isclose(events.iloc[0]["level"], 1.0040)
assert events.iloc[1]["time"] == mx.index[12] and math.isclose(events.iloc[1]["level"], 1.0050)
lab = {(r["time"], r["label"]) for _, r in labelled.iterrows() if r["label"]}
assert (mx.index[7], "HL") in lab and (mx.index[9], "HH") in lab

# --- order block: last down candle before FVG-creating displacement ----------------
small = [(1.0000 + i * 0.0002, 1.0006 + i * 0.0002, 0.9998 + i * 0.0002, 1.0005 + i * 0.0002)
         for i in range(8)]                                   # 8 small bars (~5-pip bodies)
ox = bars(small + [
    (1.0020, 1.0022, 1.0008, 1.0010),   # @8 down candle — the order block
    (1.0010, 1.0052, 1.0009, 1.0050),   # @9 up displacement (~40-pip body)
    (1.0050, 1.0060, 1.0048, 1.0058),   # @10 low 1.0048 > high@8 1.0022 → bullish FVG
])
obs = detect_order_blocks(ox)
assert len(obs) == 1
ob = obs.iloc[0]
assert ob["direction"] == "bullish" and ob["time"] == ox.index[8]
assert math.isclose(ob["top"], 1.0022) and math.isclose(ob["bottom"], 1.0008)
assert ob["confirmed_time"] == ox["close_time"].iloc[10]      # knowable only after the FVG proves it
assert len(detect_displacement(ox)) >= 1                      # @9 qualifies on its own

# --- liquidity pool: equal highs within tolerance ----------------------------------
ex = bars([
    (1.0008, 1.0010, 1.0000, 1.0006),
    (1.0006, 1.0015, 1.0002, 1.0010),
    (1.0010, 1.0030, 1.0006, 1.0020),   # swing high @2 (1.0030)
    (1.0014, 1.0016, 1.0004, 1.0008),
    (1.0008, 1.0010, 1.0002, 1.0006),
    (1.0006, 1.0014, 1.0001, 1.0010),
    (1.0010, 1.0031, 1.0005, 1.0018),   # swing high @6 (1.0031) — 1 pip apart → EQH
    (1.0010, 1.0012, 1.0003, 1.0008),
    (1.0008, 1.0010, 1.0001, 1.0006),
])
pools = detect_equal_levels(ex)
eqh = pools[pools["pool"] == "EQH"]
assert len(eqh) == 1 and math.isclose(eqh.iloc[0]["level"], 1.0031)
assert eqh.iloc[0]["side"] == "buyside"

# --- premium/discount: 50% equilibrium of the dealing range ------------------------
vis = pd.DataFrame({"time": pd.date_range("2024-01-06", periods=2, freq="1h"),
                    "kind": ["low", "high"], "level": [1.0000, 1.0100],
                    "confirmed_time": pd.date_range("2024-01-06 02:00", periods=2, freq="1h")})
pdd = premium_discount(vis, price=1.0080)
assert pdd["zone"] == "premium" and math.isclose(pdd["equilibrium"], 1.0050)
assert premium_discount(vis, price=1.0020)["zone"] == "discount"
assert premium_discount(vis, price=1.0052)["zone"] == "equilibrium"

print("hand-crafted detector fixtures: all assertions passed")

# %% [markdown]
# ### Prefix-consistency property test (no-lookahead proof)
#
# For each detector and a set of cut points over a synthetic series: records produced
# from the truncated frame must equal the full-run records filtered to
# `confirmed_time <= cut`. FVG `touch_time`/`fill_time` are excluded from the
# comparison — they legitimately summarize later bars and are only ever *compared
# against* the decision time downstream (never displayed as future knowledge).

# %%
def assert_prefix_consistent(name: str, fn: Callable[[pd.DataFrame], pd.DataFrame],
                             df: pd.DataFrame, n_cuts: int = 6,
                             drop_cols: tuple = ("touch_time", "fill_time")) -> None:
    full = fn(df)
    cuts = np.linspace(60, len(df) - 1, n_cuts, dtype=int)
    for cut in cuts:
        prefix = df.iloc[:cut]
        t = prefix["close_time"].iloc[-1]
        got = fn(prefix).drop(columns=list(drop_cols), errors="ignore").reset_index(drop=True)
        want = (full[full["confirmed_time"] <= t]
                .drop(columns=list(drop_cols), errors="ignore").reset_index(drop=True))
        pd.testing.assert_frame_equal(got, want, check_dtype=False, check_exact=False,
                                      obj=f"{name} @cut={cut}")


_syn15 = resample_ohlcv(drop_weekends(make_synthetic_1m(days=15, seed=7))[0], "15min")
assert_prefix_consistent("swings", detect_swings, _syn15)
assert_prefix_consistent("fvg", detect_fvg, _syn15)
assert_prefix_consistent("displacement", detect_displacement, _syn15)
assert_prefix_consistent("order_blocks", detect_order_blocks, _syn15)
assert_prefix_consistent("sweeps", detect_liquidity_sweeps, _syn15)
assert_prefix_consistent("structure_events", lambda d: detect_structure(d)[0], _syn15)
assert_prefix_consistent("structure_labels", lambda d: detect_structure(d)[1], _syn15)
assert_prefix_consistent("pools", detect_equal_levels, _syn15)

print(f"prefix-consistency: all 8 detectors leakage-clean over {len(_syn15):,} synthetic 15m bars")
# %% [markdown]
# ## 8 · Leakage safety — how look-ahead is prevented
#
# 1. **Decision-time convention.** A decision happens at a 15m bar close `t`. A piece
#    of information is admissible iff it was *knowable at or before* `t`.
# 2. **Closed candles only.** Every resampled bar carries `close_time = start + TF`;
#    the context builder admits bars with `close_time <= t`, so a partial current bar
#    can never leak in.
# 3. **Detector confirmation times.** Every detector record carries `confirmed_time` —
#    when the event became knowable (a k-pivot swing only k bars later; an FVG at its
#    third candle's close). Records are admitted iff `confirmed_time <= t`. The
#    prefix-consistency property test above machine-checks this for all 8 detectors.
# 4. **FVG open/filled status.** `fill_time` is precomputed over the full series for
#    speed, but is only ever **compared against `t`** (`filled iff fill_time <= t`),
#    which reveals exactly what an online observer would know at `t` — nothing more.
# 5. **Runtime guard.** `build_context()` collects every timestamp it serializes and
#    asserts `max(timestamps) < t` *(bar starts and event times all strictly precede
#    the decision close)* plus `confirmed_time <= t` for every record. A violation
#    raises `LeakageError` and kills the run.
# 6. **Next-bar fills.** Orders decided at `t` fill at the open of the *next* 1m bar —
#    the simulator never fills on a close the LLM has just seen.
# 7. **Context recency bound.** Per the requirement, context may reach back at most to
#    the start of the **previous ISO week** (Monday 00:00 UTC); intra-week bars plus
#    the week-bounded HTF view. (Structure/trend state is accumulated from earlier
#    history — past information only, which is always admissible.)

# %%
class LeakageError(AssertionError):
    """Raised when anything in a context would not have been knowable at decision time."""


def context_window_start(t: pd.Timestamp) -> pd.Timestamp:
    """Monday 00:00 UTC of the *previous* ISO week relative to t."""
    monday_this = (t - pd.Timedelta(days=int(t.dayofweek))).normalize()
    return monday_this - pd.Timedelta(weeks=1)


def visible(records: pd.DataFrame, t: pd.Timestamp,
            since: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Records knowable at t (confirmed_time <= t), optionally within the recency window."""
    if records.empty:
        return records
    m = records["confirmed_time"] <= t
    if since is not None:
        m &= records["time"] >= since
    return records[m]


# bars of each TF shown to the LLM (most recent, capped, within the week window).
# Sized for local prefill speed: ~78 tok/s on an M4-16GB makes every context byte a
# latency cost, so the bar tables stay lean and detector summaries carry the load.
BARS_IN_CONTEXT = {"5min": 12, "15min": 16, "1h": 12, "4h": 8, "1d": 6}
MAX_RECORDS_PER_TYPE = 3


def _fmt_px(x: float) -> str:
    return f"{x:.5f}"


def _fmt_bars(df: pd.DataFrame, tf: str) -> str:
    fmt = "%m-%d" if tf == "1d" else "%m-%d %H:%M"
    lines = [f"  {ts.strftime(fmt)}  O {r.open:.5f}  H {r.high:.5f}  L {r.low:.5f}  C {r.close:.5f}"
             for ts, r in zip(df.index, df.itertuples())]
    return "\n".join(lines)


def build_context(tf_store: dict, det_store: dict, t: pd.Timestamp) -> str:
    """Assemble the point-in-time multi-timeframe context block for decision time t.

    Every bar shown has close_time <= t; every detector record has confirmed_time <= t;
    nothing predates the start of the previous ISO week. A runtime leakage check runs
    before the string is returned.
    """
    t = pd.Timestamp(t)
    since = context_window_start(t)
    audit_times: list[pd.Timestamp] = []      # every timestamp that gets serialized
    sections = []

    # current price = close of the most recent closed decision-TF bar
    dec = tf_store[DECISION_TF]
    closed = dec[dec["close_time"] <= t]
    if closed.empty:
        raise LeakageError(f"no closed {DECISION_TF} bar at {t}")
    price = float(closed["close"].iloc[-1])

    ny_t = t.tz_localize("UTC").tz_convert(TZ_NY)
    header = (
        f"DECISION TIME : {t} UTC  ({ny_t.strftime('%Y-%m-%d %H:%M')} New York)\n"
        f"CURRENT PRICE : {_fmt_px(price)} (close of the last completed {DECISION_TF} bar)\n"
        f"SESSION       : New York ({NY_SESSION_START}-{NY_SESSION_END} ET) — entries allowed now\n"
        f"SPREAD        : {SPREAD_PIPS:.1f} pip fixed | history shown back to {since.date()} (previous week start)"
    )

    for tf in CONTEXT_TFS:
        bars_df = tf_store[tf]
        vis_bars = bars_df[(bars_df["close_time"] <= t) & (bars_df.index >= since)]
        vis_bars = vis_bars.tail(BARS_IN_CONTEXT[tf])
        audit_times.extend(vis_bars.index)
        d = det_store[tf]
        lines = [f"--- {tf.upper()} ---", "bars (oldest first):", _fmt_bars(vis_bars, tf)]

        ev_all = visible(d["structure"], t)            # trend = accumulated state (history)
        trend = {1: "UP", -1: "DOWN", 0: "undetermined"}[int(ev_all["trend_after"].iloc[-1])] if len(ev_all) else "undetermined"
        lines.append(f"trend after last structure event: {trend}")
        for r in ev_all[ev_all["time"] >= since].tail(3).itertuples():   # listed events stay week-bounded
            audit_times.append(r.time)
            lines.append(f"  structure: {r.kind} of {_fmt_px(r.level)} at {r.time}")

        sw = visible(d["swings"], t, since).tail(MAX_RECORDS_PER_TYPE)
        for r in sw.itertuples():
            audit_times.append(r.time)
            tag = f" ({r.label})" if isinstance(r.label, str) else ""
            lines.append(f"  swing {r.kind}{tag}: {_fmt_px(r.level)} at {r.time}")

        fv = visible(d["fvg"], t, since)
        if len(fv):
            open_mask = fv["fill_time"].isna() | (fv["fill_time"] > t)   # leakage-safe compare
            fv_open = fv[open_mask].copy()
            fv_open["dist"] = (fv_open[["top", "bottom"]].mean(axis=1) - price).abs()
            for r in fv_open.nsmallest(MAX_RECORDS_PER_TYPE, "dist").sort_values("time").itertuples():
                audit_times.append(r.time)
                touched = "touched" if (pd.notna(r.touch_time) and r.touch_time <= t) else "untouched"
                lines.append(f"  open {r.direction} FVG: {_fmt_px(r.bottom)}-{_fmt_px(r.top)} "
                             f"({touched}) formed {r.time}")

        for r in visible(d["order_blocks"], t, since).tail(MAX_RECORDS_PER_TYPE).itertuples():
            audit_times.append(r.time)
            lines.append(f"  {r.direction} order block: {_fmt_px(r.bottom)}-{_fmt_px(r.top)} at {r.time}")

        for r in visible(d["sweeps"], t, since).tail(MAX_RECORDS_PER_TYPE).itertuples():
            audit_times.append(r.time)
            lines.append(f"  liquidity sweep ({r.side}): level {_fmt_px(r.swept_level)} "
                         f"wick {_fmt_px(r.wick_extreme)} at {r.time}")

        for r in visible(d["pools"], t, since).tail(MAX_RECORDS_PER_TYPE).itertuples():
            audit_times.append(r.time)
            lines.append(f"  liquidity pool {r.pool} ({r.side} resting stops): {_fmt_px(r.level)}")

        pdd = premium_discount(visible(d["swings"], t, since), price)
        if pdd:
            lines.append(f"  dealing range {_fmt_px(pdd['range_low'])}-{_fmt_px(pdd['range_high'])}, "
                         f"equilibrium {_fmt_px(pdd['equilibrium'])} → price in {pdd['zone'].upper()} "
                         f"({pdd['position']:.0%} of range)")

        for r in visible(d["displacement"], t, since).tail(2).itertuples():
            audit_times.append(r.time)
            lines.append(f"  displacement ({r.direction}) at {r.time}")

        sections.append("\n".join(lines))

    # ---- runtime leakage guard: everything serialized must predate the decision close
    if audit_times:
        worst = max(audit_times)
        if not worst < t:
            raise LeakageError(f"context for {t} contains timestamp {worst} >= decision time")
    return header + "\n\n" + "\n\n".join(sections)

print("context builder ready")

# %% [markdown]
# ## 9 · LLM decision layer
#
# The system prompt is an ICT primer distilled from the same researched sources as the
# detectors, plus a strict output contract. Generation parameters are pinned
# (temperature 0, fixed seed) and JSON output is requested natively from the provider,
# so a given context string reproducibly maps to the same `TradePlan`.
#
# **Provider note:** `LLM_PROVIDER` in the config selects local Ollama (the original
# spec: local LLM, no internet, no paid APIs) or the Gemini API — a **documented
# temporary exception** taken because this machine sustains only ~35-45s per local
# 8B call, while reasoning quality was the priority. Both providers share the same
# prompt, validator, cache and audit-log path, so results stay comparable.

# %%
SYSTEM_PROMPT = f"""You are a disciplined intraday EUR/USD trader using ICT (Inner Circle Trader) /
Smart Money Concepts. You receive a point-in-time multi-timeframe market snapshot and must decide:
long, short, or none.

CONCEPT PRIMER (how to read the context):
- Fair Value Gap (FVG): a 3-candle imbalance left by a displacement candle. Price frequently
  retraces into an open FVG before continuing. An untouched FVG in your trade direction is
  a high-quality entry zone.
- Order Block (OB): the last opposing candle before a displacement that created an FVG —
  where institutional orders entered. Expect reactions when price returns to the zone.
- Liquidity Sweep: a wick through a prior swing high/low that closes back inside — a stop
  raid. A sell-side sweep (below lows) is bullish; a buy-side sweep (above highs) is bearish.
- BOS (Break of Structure): close beyond a swing WITH the trend — continuation.
  CHoCH (Change of Character): close beyond a counter-trend swing — first reversal warning.
- Liquidity pools (EQH/EQL): equal highs/lows where stops cluster; price gravitates there.
- Premium/Discount: equilibrium is 50% of the dealing range. Prefer LONGS in DISCOUNT and
  SHORTS in PREMIUM. Avoid chasing entries deep against this rule.
- The classic A+ sequence: liquidity sweep -> CHoCH/MSS with displacement -> entry on the
  retrace into the FVG/OB, stop beyond the sweep wick, in the right premium/discount zone,
  aligned with the higher-timeframe trend.

DECISION RULES (binding):
1. Direction "none" is the correct call most of the time. Only trade clear multi-timeframe
   confluence: HTF (1h/4h/1d) bias + a recent sweep or structure shift on 5m/15m + sensible
   premium/discount location.
2. Entry must be a market order at (or within a few pips of) CURRENT PRICE — fills happen
   on the next bar. Do not place limit orders far from price.
3. stop_loss goes beyond a real structural level (sweep wick, OB edge, FVG far edge),
   on the correct side of entry.
4. take_profit MUST equal entry +/- {RR_TARGET:.0f} x (the entry-to-stop distance): exactly
   1:{RR_TARGET:.0f} risk:reward. Plans violating this are rejected.
5. confidence is 0-100. Use >= {CONF_THRESHOLD} ONLY for A+ setups (rule 1 fully satisfied).
   Trades below {CONF_THRESHOLD} are skipped automatically — be honest, not eager.
6. Costs: spread is {SPREAD_PIPS:.1f} pip; stops tighter than ~5 pips are noise — avoid them.

OUTPUT CONTRACT — respond with ONLY this JSON object, no code fences, no extra text:
{{"direction": "long" | "short" | "none",
  "entry": <float>, "stop_loss": <float>, "take_profit": <float>,
  "confidence": <int 0-100>, "reasoning": "<one or two short sentences>"}}
For "none", entry/stop_loss/take_profit may be 0."""


@dataclass
class TradePlan:
    direction: str                    # "long" | "short" | "none"
    entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    confidence: int = 0
    reasoning: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def ollama_generate(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """One pinned-parameter, non-streaming Ollama call. Text in, text out.
    `keep_alive` holds the model in memory between calls (on a 16GB machine Ollama
    otherwise unloads it, costing a ~12s reload and the occasional dropped
    connection). Transient connection errors are retried with backoff — distinct
    from the validator's semantic retry."""
    est_tokens = (len(system) + len(prompt)) // 3 + OLLAMA_PARAMS["num_predict"]
    num_ctx = 4096 if est_tokens < 3800 else 8192      # smaller KV cache when it fits —
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "system": system,    # this is a
               "stream": False, "format": "json", "keep_alive": "60m",       # 16GB machine
               "options": {**OLLAMA_PARAMS, "num_ctx": num_ctx}}
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(3):
        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            r.raise_for_status()
            return r.json()["response"]
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            _time.sleep(2 * (attempt + 1))
    raise last_err


_gemini_last_call = 0.0

def gemini_generate(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """Gemini API call with the same contract as `ollama_generate` (text in, text out).
    TEMPORARY EXCEPTION to the original local-only/no-paid-API spec — adopted for
    reasoning quality + speed on memory-constrained hardware; see the config cell.
    Pinned params + JSON response mime type; 429/5xx retried with backoff (free-tier
    rate limits surface as 429s). Calls are spaced GEMINI_MIN_INTERVAL_S apart so a
    free-tier run paces under the RPM cap instead of burning its capped retries."""
    global _gemini_last_call
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set — export it or set LLM_PROVIDER='ollama'")
    wait = GEMINI_MIN_INTERVAL_S - (_time.time() - _gemini_last_call)
    if wait > 0:
        _time.sleep(wait)
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {**GEMINI_PARAMS, "responseMimeType": "application/json"}}
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(4):
        try:
            _gemini_last_call = _time.time()
            r = requests.post(GEMINI_URL, json=body, timeout=GEMINI_TIMEOUT,
                              headers={"x-goog-api-key": GEMINI_API_KEY})
            if r.status_code in (429, 500, 502, 503):
                last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                _time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            cands = r.json().get("candidates", [])
            if not cands or "content" not in cands[0]:
                raise RuntimeError(f"no candidates in response: {r.text[:200]}")
            return "".join(p.get("text", "") for p in cands[0]["content"].get("parts", []))
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            _time.sleep(5 * (attempt + 1))
    raise last_err


DEFAULT_GENERATE: Callable[[str], str] = (
    gemini_generate if LLM_PROVIDER == "gemini" else ollama_generate)
ACTIVE_LLM_PARAMS = GEMINI_PARAMS if LLM_PROVIDER == "gemini" else OLLAMA_PARAMS


def parse_trade_plan(text: str) -> tuple[Optional[TradePlan], str]:
    """Defensive parse: strip code fences, isolate the outermost JSON object, coerce
    types, clamp confidence to [0, 100]. Returns (plan, "") or (None, reason)."""
    s = re.sub(r"```(?:json)?", "", text).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None, "no JSON object found"
    s = s[start:end + 1]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        try:
            obj = json.loads(re.sub(r",\s*([}\]])", r"\1", s))   # drop trailing commas
        except json.JSONDecodeError as e:
            return None, f"malformed JSON: {e}"
    if not isinstance(obj, dict):
        return None, "JSON is not an object"
    direction = str(obj.get("direction", "")).lower().strip()
    if direction not in ("long", "short", "none"):
        return None, f"bad direction {obj.get('direction')!r}"
    try:
        plan = TradePlan(
            direction=direction,
            entry=float(obj.get("entry") or 0.0),
            stop_loss=float(obj.get("stop_loss") or 0.0),
            take_profit=float(obj.get("take_profit") or 0.0),
            confidence=int(max(0, min(100, float(obj.get("confidence") or 0)))),
            reasoning=str(obj.get("reasoning", ""))[:500],
        )
    except (TypeError, ValueError) as e:
        return None, f"bad field types: {e}"
    return plan, ""

print("LLM layer ready —", LLM_MODEL_ID)

# %% [markdown]
# ## 10 · Validator
#
# A plan is **rejected** when the implied reward:risk violates the configured 1:2
# (outside `RR_TARGET ± RR_TOLERANCE`), when the levels are on the wrong sides of the
# entry, or when the entry strays so far from current price that a next-bar market fill
# would not resemble it. On rejection the LLM is re-prompted **once** with the reason;
# a second failure skips the trade (`direction: none`) and logs why.

# %%
MAX_ENTRY_DRIFT_PIPS = 10.0   # entry must be a market-order near current price

def validate_plan(plan: TradePlan, price: float) -> tuple[bool, str]:
    if plan.direction == "none":
        return True, "no-trade"
    e, sl, tp = plan.entry, plan.stop_loss, plan.take_profit
    if min(e, sl, tp) <= 0:
        return False, "non-positive level(s)"
    if plan.direction == "long" and not (sl < e < tp):
        return False, f"long needs SL<entry<TP, got SL={sl} E={e} TP={tp}"
    if plan.direction == "short" and not (tp < e < sl):
        return False, f"short needs TP<entry<SL, got TP={tp} E={e} SL={sl}"
    risk, reward = abs(e - sl), abs(tp - e)
    if risk < 1e-9:
        return False, "zero risk distance"
    rr = reward / risk
    # BOT_RR_FREE: liquidity-target strategies (BLOODHOUND, MIDNIGHT RAID) have variable
    # RR by design, so skip the fixed-RR check but still require a minimum 1.0R.
    if RR_FREE:
        if rr < 1.0:
            return False, f"R:R {rr:.2f} below 1.0 minimum"
    elif abs(rr - RR_TARGET) > RR_TOLERANCE:
        return False, f"R:R {rr:.2f} violates target {RR_TARGET:.1f}±{RR_TOLERANCE}"
    if abs(e - price) > MAX_ENTRY_DRIFT_PIPS * PIP:
        return False, (f"entry {e:.5f} is {abs(e - price) / PIP:.1f} pips from current "
                       f"price {price:.5f} (max {MAX_ENTRY_DRIFT_PIPS:.0f})")
    return True, "ok"

# quick self-checks (valid under both fixed-RR and BOT_RR_FREE modes)
_risk = 0.0010
_on_tgt = TradePlan("long", 1.1000, 1.1000 - _risk, 1.1000 + RR_TARGET * _risk, 80)
_lowrr = TradePlan("long", 1.1000, 1.1000 - _risk, 1.1000 + 0.5 * _risk, 80)   # 0.5R: fails both modes
assert validate_plan(_on_tgt, 1.1001)[0]
assert not validate_plan(_lowrr, 1.1001)[0]                                          # bad R:R
assert not validate_plan(TradePlan("short", 1.1000, 1.0990, 1.1020, 80), 1.1001)[0]  # sides wrong
assert not validate_plan(_on_tgt, 1.1050)[0]                                         # entry drift
assert validate_plan(TradePlan("none"), 1.1)[0]
print("validator ready")

# %% [markdown]
# ## 11 · Cache & audit log
#
# - **Cache:** responses keyed by SHA-256 of (model + pinned params + system prompt +
#   exact context string). Identical context → identical TradePlan with zero recompute.
#   The cached record stores the *final* post-retry outcome.
# - **Audit log:** every attempt (including retries and cache hits) appends a JSONL
#   line with the raw prompt, raw response, parsed plan, validation verdict and latency.

# %%
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

class LLMCache:
    def __init__(self, root: Path):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.hits = self.misses = 0

    @staticmethod
    def key(context: str, system: str = SYSTEM_PROMPT) -> str:
        blob = "|".join([LLM_MODEL_ID, json.dumps(ACTIVE_LLM_PARAMS, sort_keys=True),
                         system, context])
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> Optional[dict]:
        p = self.root / f"{key}.json"
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text())
        self.misses += 1
        return None

    def put(self, key: str, record: dict) -> None:
        (self.root / f"{key}.json").write_text(json.dumps(record, default=str))


class JsonlLogger:
    def __init__(self, path: Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def log(self, **fields) -> None:
        fields["logged_at"] = datetime.now().isoformat(timespec="seconds")
        with self.path.open("a") as f:
            f.write(json.dumps(fields, default=str) + "\n")
        self.n += 1


def llm_decide(context: str, price: float, cache: LLMCache, logger: JsonlLogger,
               generate_fn: Optional[Callable[[str], str]] = None,
               decision_time=None) -> dict:
    """Cached, validated, retry-once LLM decision. Returns
    {plan, status, cache_hit, attempts, reject_reason}."""
    generate_fn = generate_fn or DEFAULT_GENERATE
    key = cache.key(context)
    cached = cache.get(key)
    if cached is not None:
        logger.log(event="cache_hit", decision_time=decision_time, key=key,
                   plan=cached["plan"], status=cached["status"])
        return {"plan": TradePlan(**cached["plan"]), "status": cached["status"],
                "cache_hit": True, "attempts": 0, "reject_reason": cached.get("reject_reason", "")}

    prompt, status, reject_reason = context, "", ""
    plan: Optional[TradePlan] = None
    for attempt in (1, 2):
        t0 = _time.time()
        try:
            raw = generate_fn(prompt)
        except Exception as e:                      # Ollama down / timeout → no trade
            logger.log(event="llm_error", decision_time=decision_time, key=key,
                       attempt=attempt, error=repr(e))
            plan, status, reject_reason = TradePlan("none"), "llm_error", repr(e)
            break
        latency = _time.time() - t0
        cand, perr = parse_trade_plan(raw)
        if cand is None:
            ok, reason = False, f"parse failure: {perr}"
        else:
            ok, reason = validate_plan(cand, price)
        logger.log(event="llm_call", decision_time=decision_time, key=key, attempt=attempt,
                   raw_prompt=prompt, raw_response=raw, latency_s=round(latency, 2),
                   parsed=cand.to_dict() if cand else None, valid=ok, reason=reason)
        if ok:
            plan, status = cand, "ok"
            break
        if attempt == 1:                            # retry once, with the reason inline
            prompt = (context + "\n\nYOUR PREVIOUS RESPONSE WAS REJECTED: " + reason +
                      "\nRespond again. Output ONLY the JSON object, exactly per the contract.")
            status, reject_reason = "retried", reason
        else:                                       # second failure → skip the trade
            plan, status, reject_reason = TradePlan("none"), "validator_rejected", reason
    if status != "llm_error":          # never cache transport failures — a re-run
        record = {"plan": plan.to_dict(), "status": status,        # should retry them
                  "reject_reason": reject_reason, "decision_time": str(decision_time)}
        cache.put(key, record)
    return {"plan": plan, "status": status, "cache_hit": False,
            "attempts": attempt, "reject_reason": reject_reason}

print(f"cache + logger ready (run id {RUN_ID})")
# %% [markdown]
# ## 12 · Execution simulator (paper)
#
# Streams **1-minute** bars in strict order — intrabar SL/TP resolution happens at 1m
# granularity even though decisions fire on 15m closes.
#
# - **Fills:** a plan decided at close `t` becomes a market order filled at the **next
#   1m bar's open**. The simulator never fills on the close the LLM just saw.
# - **Costs:** spread only — a flat `SPREAD_PIPS` deduction per round trip (prices are
#   mid; triggers evaluate on mid). No slippage, no commission, per spec.
# - **Conservatism:** if a 1m bar touches both SL and TP, the **SL is assumed to hit
#   first** (pessimistic). If the fill-bar open already gaps beyond SL or TP, the order
#   is cancelled and logged rather than filled into a degenerate position.
# - **Sizing:** `units = (equity × RISK_PCT) / |fill − SL|` — exactly 1% of current
#   equity at risk; confidence never scales size (it is a gate only).
# - **One position at a time**; positions may carry outside the session until SL/TP.
# - **Circuit breakers** (all logged): daily loss ≥ 3% of start-of-day equity → no new
#   entries until the next NY day; ≥ 5 consecutive losses → paused for the rest of the
#   day (counter carries overnight); peak-to-trough drawdown ≥ 15% → the run halts
#   permanently. Breaker-blocked decision slots skip the LLM call entirely.

# %%
def _session_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

_SESS_LO = _session_minutes(NY_SESSION_START)
_SESS_HI = _session_minutes(NY_SESSION_END)

def in_ny_session(times: pd.DatetimeIndex) -> np.ndarray:
    """True where a (tz-naive UTC) timestamp falls inside the NY entry session."""
    ny = times.tz_localize("UTC").tz_convert(TZ_NY)
    hm = ny.hour * 60 + ny.minute
    return np.asarray((ny.dayofweek < 5) & (hm >= _SESS_LO) & (hm < _SESS_HI))


def prepare_market(df_1m: pd.DataFrame, intraday_start: Optional[pd.Timestamp] = None
                   ) -> tuple[dict, dict]:
    """Resample once per TF and run every detector once. HTF frames (1h/4h/1d) keep
    full history (structure state benefits from a long warm-up); the heavy intraday
    frames (5m/15m) may be restricted to `intraday_start` (give it a generous buffer
    before the backtest window — detector warm-up only, contexts stay week-bounded)."""
    frames, dets = {}, {}
    for tf in CONTEXT_TFS:
        src = df_1m
        if intraday_start is not None and tf in ("5min", "15min"):
            src = df_1m[df_1m.index >= intraday_start]
        frames[tf] = resample_ohlcv(src, tf)
    for tf in CONTEXT_TFS:
        dets[tf] = run_all_detectors(frames[tf])
    return frames, dets


def run_engine(df_1m: pd.DataFrame, tf_store: dict, det_store: dict,
               decide_fn: Callable, start: pd.Timestamp, end: pd.Timestamp,
               label: str, equity0: float = ACCOUNT_EQUITY,
               max_decisions: Optional[int] = None, verbose: bool = False,
               peak0: Optional[float] = None) -> dict:
    """Event-driven paper engine over [start, end). `decide_fn(ctx_provider, price, t)`
    must return the dict produced by `llm_decide` (or a compatible strategy)."""
    sl_1m = df_1m[(df_1m.index >= start) & (df_1m.index < end)]
    times = sl_1m.index
    o, h, l, c = (sl_1m[k].to_numpy() for k in ("open", "high", "low", "close"))
    bar_close = (times + pd.Timedelta("1min")).asi8          # ns ints for set lookups

    dec = tf_store[DECISION_TF]
    ct = dec["close_time"]
    in_window = (ct > start) & (ct <= end)
    sess = pd.Series(in_ny_session(pd.DatetimeIndex(ct)), index=ct.index)
    all_closes = set(pd.DatetimeIndex(ct[in_window]).asi8)                # equity marks
    decision_set = set(pd.DatetimeIndex(ct[in_window & sess]).asi8)       # entry slots

    equity = float(equity0)
    peak = float(peak0) if peak0 is not None else equity                  # carries across folds
    position = pending = None
    trades, curve, halts = [], [], []
    counters = {"decision_slots": 0, "llm_consults": 0, "no_trade": 0, "gated": 0,
                "validator_rejected": 0, "llm_errors": 0, "retries": 0, "cache_hits": 0,
                "orders": 0, "fills": 0, "cancelled_gap": 0, "breaker_day_blocks": 0,
                "breaker_pause_blocks": 0, "breaker_halt_blocks": 0}
    cur_day = None
    day_start_equity = equity
    day_blocked = paused = halted = False
    consec_losses = 0

    def close_trade(i: int, px: float, reason: str) -> None:
        nonlocal position, equity, consec_losses, peak, day_blocked, paused, halted
        sign = 1.0 if position["direction"] == "long" else -1.0
        pnl = (px - position["entry"]) * sign * position["units"] \
              - SPREAD_PIPS * PIP * position["units"]            # round-trip spread
        equity += pnl
        trades.append({**position, "exit_time": times[i], "exit_px": px, "reason": reason,
                       "pnl_usd": pnl, "r_multiple": pnl / position["risk_usd"],
                       "equity_after": equity})
        consec_losses = consec_losses + 1 if pnl < 0 else 0
        if verbose:
            print(f"  [{times[i]}] EXIT {position['direction']} @ {px:.5f} ({reason}) "
                  f"pnl ${pnl:,.0f} → equity ${equity:,.0f}")
        position = None
        peak = max(peak, equity)
        if equity - day_start_equity <= -DAILY_LOSS_LIMIT * day_start_equity and not day_blocked:
            day_blocked = True
            halts.append({"time": times[i], "kind": "daily_loss_limit",
                          "detail": f"day pnl {equity - day_start_equity:,.0f}"})
        if consec_losses >= MAX_CONSEC_LOSSES and not paused:
            paused = True
            halts.append({"time": times[i], "kind": "consec_loss_pause",
                          "detail": f"{consec_losses} straight losses"})
        if (peak - equity) / peak >= MAX_DRAWDOWN_HALT and not halted:
            halted = True
            halts.append({"time": times[i], "kind": "max_drawdown_halt",
                          "detail": f"drawdown {(peak - equity) / peak:.1%}"})

    for i in range(len(times)):
        # 1 · fill pending order at this bar's open (the bar after the decision)
        if pending is not None and not halted:
            p = pending; pending = None
            fill = o[i]
            bad_gap = (p["direction"] == "long" and (fill <= p["stop_loss"] or fill >= p["take_profit"])) or \
                      (p["direction"] == "short" and (fill >= p["stop_loss"] or fill <= p["take_profit"]))
            if bad_gap:
                counters["cancelled_gap"] += 1
            else:
                risk_usd = equity * RISK_PCT
                units = risk_usd / abs(fill - p["stop_loss"])
                position = {"strategy": label, "direction": p["direction"],
                            "entry_time": times[i], "entry": fill,
                            "stop_loss": p["stop_loss"], "take_profit": p["take_profit"],
                            "units": units, "risk_usd": risk_usd,
                            "confidence": p["confidence"], "reasoning": p["reasoning"],
                            "decided_at": p["decided_at"]}
                counters["fills"] += 1
                if verbose:
                    print(f"  [{times[i]}] FILL {p['direction']} @ {fill:.5f} "
                          f"SL {p['stop_loss']:.5f} TP {p['take_profit']:.5f}")
        elif pending is not None:
            pending = None                                   # halted while order in flight

        # 2 · manage the open position on this 1m bar (SL first — pessimistic)
        if position is not None:
            if position["direction"] == "long":
                if l[i] <= position["stop_loss"]:
                    close_trade(i, position["stop_loss"], "SL")
                elif h[i] >= position["take_profit"]:
                    close_trade(i, position["take_profit"], "TP")
            else:
                if h[i] >= position["stop_loss"]:
                    close_trade(i, position["stop_loss"], "SL")
                elif l[i] <= position["take_profit"]:
                    close_trade(i, position["take_profit"], "TP")

        bt = bar_close[i]
        # 3 · decision slot at a session 15m close, flat & unblocked, breakers willing
        if bt in decision_set:
            t = times[i] + pd.Timedelta("1min")
            day = t.tz_localize("UTC").tz_convert(TZ_NY).date()
            if day != cur_day:                               # NY-day rollover
                cur_day, day_start_equity = day, equity
                day_blocked = paused = False
            counters["decision_slots"] += 1
            if max_decisions is not None and counters["llm_consults"] >= max_decisions:
                pass
            elif halted:
                counters["breaker_halt_blocks"] += 1
            elif day_blocked:
                counters["breaker_day_blocks"] += 1
            elif paused:
                counters["breaker_pause_blocks"] += 1
            elif position is None and pending is None:
                price = float(c[i])
                ctx_provider = (lambda tt=t: build_context(tf_store, det_store, tt))
                res = decide_fn(ctx_provider, price, t)
                counters["llm_consults"] += 1
                counters["cache_hits"] += int(res.get("cache_hit", False))
                counters["retries"] += max(0, res.get("attempts", 1) - 1)
                plan, status = res["plan"], res["status"]
                if status == "validator_rejected":
                    counters["validator_rejected"] += 1
                elif status == "llm_error":
                    counters["llm_errors"] += 1
                elif plan.direction == "none":
                    counters["no_trade"] += 1
                elif plan.confidence < CONF_THRESHOLD:
                    counters["gated"] += 1                   # confidence gate — no entry
                else:
                    pending = {"direction": plan.direction, "stop_loss": plan.stop_loss,
                               "take_profit": plan.take_profit, "confidence": plan.confidence,
                               "reasoning": plan.reasoning, "decided_at": t}
                    counters["orders"] += 1
                    if verbose:
                        print(f"[{t}] ORDER {plan.direction} conf {plan.confidence} — "
                              f"{plan.reasoning[:90]}")

        # 4 · mark-to-market equity at every 15m close in the window
        if bt in all_closes:
            mtm = equity
            if position is not None:
                sign = 1.0 if position["direction"] == "long" else -1.0
                mtm += (c[i] - position["entry"]) * sign * position["units"]
            curve.append((times[i] + pd.Timedelta("1min"), mtm))

    if position is not None:                                 # end of run: mark flat
        close_trade(len(times) - 1, float(c[-1]), "end_of_run")

    trades_df = pd.DataFrame(trades)
    curve_df = pd.DataFrame(curve, columns=["time", "equity"]).set_index("time")
    return {"label": label, "trades": trades_df, "curve": curve_df,
            "counters": counters, "halts": halts, "final_equity": equity,
            "final_peak": peak, "halted": halted}

print("simulator ready")
# %% [markdown]
# ## 13 · Strategies
#
# Three deciders share the engine's single code path:
#
# 1. **LLM strategy** — context → Ollama → validated `TradePlan` (the system under test).
# 2. **Rule-only ICT baseline** — the *primary comparison*: a mechanical version of the
#    exact confluence the LLM is asked to judge (sweep → structure shift → premium/
#    discount filter), isolating whether the LLM adds value over its own inputs.
# 3. **Mock LLM** — deterministic scripted responses (including malformed JSON and an
#    R:R violation) that exercise the parser, validator, retry and confidence-gate
#    paths in the smoke test without an Ollama server.
#
# Plus a **buy-and-hold** reference (flat-ish for FX; included for completeness).

# %%
def make_llm_decide_fn(cache: LLMCache, logger: JsonlLogger,
                       generate_fn: Optional[Callable[[str], str]] = None) -> Callable:
    def decide(ctx_provider, price, t):
        return llm_decide(ctx_provider(), price, cache, logger,
                          generate_fn=generate_fn or DEFAULT_GENERATE, decision_time=t)
    return decide


# --- rule-only ICT baseline ---------------------------------------------------------
RULE_SWEEP_WINDOW = pd.Timedelta("3h")   # sweep must be at most this old (12 × 15m bars)
RULE_MIN_STOP_PIPS = 5.0
RULE_SL_BUFFER_PIPS = 2.0

def make_rule_decide_fn(det_store: dict) -> Callable:
    """Mechanical ICT confluence, mirroring the LLM's A+ template:
    recent 15m liquidity sweep → 15m structure shift in the sweep's reversal direction
    confirmed after the sweep → 1h premium/discount filter. SL beyond the sweep wick,
    TP at exactly 1:RR_TARGET. No confidence notion (always passes the gate)."""
    d15, d1h = det_store["15min"], det_store["1h"]

    def decide(ctx_provider, price, t):
        none = {"plan": TradePlan("none"), "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
        sweeps = visible(d15["sweeps"], t, since=t - RULE_SWEEP_WINDOW)
        if sweeps.empty:
            return none
        s = sweeps.iloc[-1]
        want = "long" if s["side"] == "sellside" else "short"
        ev = visible(d15["structure"], t)
        ev_after = ev[ev["time"] > s["time"]]
        shift_kinds = ("CHoCH_up", "BOS_up") if want == "long" else ("CHoCH_down", "BOS_down")
        if ev_after.empty or not ev_after["kind"].isin(shift_kinds).any():
            return none
        pdd = premium_discount(visible(d1h["swings"], t, since=context_window_start(t)), price)
        if pdd is None or (want == "long" and pdd["zone"] != "discount") \
                or (want == "short" and pdd["zone"] != "premium"):
            return none
        buf = RULE_SL_BUFFER_PIPS * PIP
        sl = s["wick_extreme"] - buf if want == "long" else s["wick_extreme"] + buf
        risk = (price - sl) if want == "long" else (sl - price)
        if risk < RULE_MIN_STOP_PIPS * PIP:
            return none
        tp = price + RR_TARGET * risk if want == "long" else price - RR_TARGET * risk
        plan = TradePlan(want, entry=price, stop_loss=sl, take_profit=tp,
                         confidence=100, reasoning="rule: sweep→shift→P/D confluence")
        ok, reason = validate_plan(plan, price)
        if not ok:
            return {**none, "status": "validator_rejected", "reject_reason": reason}
        return {"plan": plan, "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
    return decide


# --- improved ICT: breakout/continuation + higher-timeframe bias --------------------
# The best out-of-sample config from the tune_rules.py study (Test 5 in
# RESEARCH_SUMMARY): trade WITH a recent 15m displacement, but only when it agrees with
# the 1h trend (proper-ICT HTF bias), confirmed by a same-direction structure shift, in
# the favorable 4h premium/discount zone; stop beyond the last opposing 15m swing.
# Engine-side port of tune_rules.py so it can produce a full notebook report; uses
# RR_TARGET (set BOT_RR=1.5 to match the validated config).
BRK_SIGNAL_WINDOW = pd.Timedelta("2h")
BRK_MIN_STOP_PIPS = 8.0

def make_breakout_decide_fn(det_store: dict, htf_bias_tf: str = "1h",
                            pd_tf: str = "4h") -> Callable:
    d15, dbias, dpd = det_store["15min"], det_store[htf_bias_tf], det_store[pd_tf]

    def decide(ctx_provider, price, t):
        none = {"plan": TradePlan("none"), "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
        if not (7 * 60 <= _et_minute(t) < 16 * 60):          # NY session self-gate
            return none
        disp = visible(d15["displacement"], t, since=t - BRK_SIGNAL_WINDOW)
        if disp.empty:
            return none
        want = "long" if disp.iloc[-1]["direction"] == "bullish" else "short"
        d_time = disp.iloc[-1]["time"]
        # HTF bias: only trade with the 1h trend
        bias = visible(dbias["structure"], t)
        if bias.empty:
            return none
        trend = bias.iloc[-1]["trend_after"]
        if (want == "long" and trend != 1) or (want == "short" and trend != -1):
            return none
        # same-direction structure shift after the displacement
        ev_after = visible(d15["structure"], t)
        ev_after = ev_after[ev_after["time"] > d_time]
        shift_kinds = ("CHoCH_up", "BOS_up") if want == "long" else ("CHoCH_down", "BOS_down")
        if ev_after.empty or not ev_after["kind"].isin(shift_kinds).any():
            return none
        pdd = premium_discount(visible(dpd["swings"], t, since=context_window_start(t)), price)
        if pdd is None or (want == "long" and pdd["zone"] != "discount") \
                or (want == "short" and pdd["zone"] != "premium"):
            return none
        # stop beyond the last opposing 15m swing
        sw = visible(d15["swings"], t, since=context_window_start(t))
        sw = sw[sw["kind"] == ("low" if want == "long" else "high")]
        if sw.empty:
            return none
        ref = sw.iloc[-1]["level"]
        buf = RULE_SL_BUFFER_PIPS * PIP
        sl = ref - buf if want == "long" else ref + buf
        risk = (price - sl) if want == "long" else (sl - price)
        if risk < BRK_MIN_STOP_PIPS * PIP:
            return none
        tp = price + RR_TARGET * risk if want == "long" else price - RR_TARGET * risk
        plan = TradePlan(want, entry=price, stop_loss=sl, take_profit=tp,
                         confidence=100, reasoning="breakout + 1h HTF bias + P/D")
        ok, reason = validate_plan(plan, price)
        if not ok:
            return {**none, "status": "validator_rejected", "reject_reason": reason}
        return {"plan": plan, "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
    return decide


# --- liquidity references for the new strategies ------------------------------------
def _et_minute(t) -> int:
    """Minute-of-day in New York time for a tz-naive UTC timestamp."""
    e = pd.Timestamp(t).tz_localize("UTC").tz_convert(TZ_NY)
    return e.hour * 60 + e.minute

def _prev_day_hilo_lookup(tf_store: dict):
    """f(t) -> (high, low) of the most recent *completed* daily bar (knowable at t)."""
    d1 = tf_store["1d"]
    ct = d1["close_time"].to_numpy(dtype="datetime64[ns]")
    hi, lo = d1["high"].to_numpy(), d1["low"].to_numpy()
    def f(t):
        i = int(np.searchsorted(ct, np.datetime64(t), side="right")) - 1
        return (hi[i], lo[i]) if i >= 0 else None
    return f

def _midnight_open_lookup(tf_store: dict):
    """f(t) -> open of the current ET day's 00:00 15m bar (the Judas reference)."""
    f15 = tf_store["15min"]
    et = f15.index.tz_localize("UTC").tz_convert(TZ_NY)
    mid = np.asarray((et.hour == 0) & (et.minute == 0))
    dates = et[mid].normalize().tz_localize(None)
    m = {d: o for d, o in zip([x.date() for x in dates], f15["open"].to_numpy()[mid])}
    def f(t):
        return m.get(pd.Timestamp(t).tz_localize("UTC").tz_convert(TZ_NY).date())
    return f


# --- MIDNIGHT RAID — ICT Judas Swing (session-open liquidity raid) -------------------
JUDAS_LO, JUDAS_HI = 0, 5 * 60           # 00:00–05:00 ET window

def make_judas_decide_fn(det_store: dict, tf_store: dict, htf_bias_tf: str = "1h") -> Callable:
    d15, dbias = det_store["15min"], det_store[htf_bias_tf]
    f15 = tf_store["15min"]
    idx = f15.index.to_numpy(dtype="datetime64[ns]")
    low, high = f15["low"].to_numpy(), f15["high"].to_numpy()
    mid_open, pdhl = _midnight_open_lookup(tf_store), _prev_day_hilo_lookup(tf_store)

    def decide(ctx_provider, price, t):
        none = {"plan": TradePlan("none"), "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
        if not (JUDAS_LO <= _et_minute(t) < JUDAS_HI):       # 00:00–05:00 ET only
            return none
        mo = mid_open(t)
        if mo is None:
            return none
        bias = visible(dbias["structure"], t)
        if bias.empty:
            return none
        trend = bias.iloc[-1]["trend_after"]
        want = "long" if trend == 1 else "short"
        # today's bars from 00:00 ET up to t — find the judas extreme against bias
        t_et = pd.Timestamp(t).tz_localize("UTC").tz_convert(TZ_NY)
        day0 = np.datetime64(t_et.normalize().tz_convert("UTC").tz_localize(None))
        m = (idx >= day0) & (idx <= np.datetime64(t))
        if not m.any():
            return none
        if want == "long":
            ext = low[m].min()
            if ext >= mo:                                    # no sweep below the open
                return none
            ext_time = pd.Timestamp(idx[m][low[m].argmin()])
        else:
            ext = high[m].max()
            if ext <= mo:                                    # no sweep above the open
                return none
            ext_time = pd.Timestamp(idx[m][high[m].argmax()])
        # reversal: same-as-bias 15m structure shift after the judas extreme
        ev = visible(d15["structure"], t)
        ev = ev[ev["time"] > ext_time]
        shift = ("CHoCH_up", "BOS_up") if want == "long" else ("CHoCH_down", "BOS_down")
        if ev.empty or not ev["kind"].isin(shift).any():
            return none
        buf = RULE_SL_BUFFER_PIPS * PIP
        sl = ext - buf if want == "long" else ext + buf
        risk = (price - sl) if want == "long" else (sl - price)
        if risk < RULE_MIN_STOP_PIPS * PIP:
            return none
        pdh = pdhl(t)
        if pdh is None:
            return none
        tp = pdh[0] if want == "long" else pdh[1]            # previous-day liquidity
        reward = (tp - price) if want == "long" else (price - tp)
        if reward < risk:                                    # < 1R to the liquidity pool
            return none
        plan = TradePlan(want, entry=price, stop_loss=sl, take_profit=tp,
                         confidence=100, reasoning="judas: sweep midnight open → MSS → PDH/PDL")
        ok, reason = validate_plan(plan, price)
        if not ok:
            return {**none, "status": "validator_rejected", "reject_reason": reason}
        return {"plan": plan, "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
    return decide


# --- BLOODHOUND — SLIPSTREAM entry, previous-day-liquidity target --------------------
def make_breakout_liq_decide_fn(det_store: dict, tf_store: dict, htf_bias_tf: str = "1h",
                                pd_tf: str = "4h") -> Callable:
    d15, dbias, dpd = det_store["15min"], det_store[htf_bias_tf], det_store[pd_tf]
    pdhl = _prev_day_hilo_lookup(tf_store)

    def decide(ctx_provider, price, t):
        none = {"plan": TradePlan("none"), "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
        if not (7 * 60 <= _et_minute(t) < 16 * 60):          # NY session self-gate
            return none
        disp = visible(d15["displacement"], t, since=t - BRK_SIGNAL_WINDOW)
        if disp.empty:
            return none
        want = "long" if disp.iloc[-1]["direction"] == "bullish" else "short"
        d_time = disp.iloc[-1]["time"]
        bias = visible(dbias["structure"], t)
        if bias.empty:
            return none
        trend = bias.iloc[-1]["trend_after"]
        if (want == "long" and trend != 1) or (want == "short" and trend != -1):
            return none
        ev = visible(d15["structure"], t)
        ev = ev[ev["time"] > d_time]
        shift = ("CHoCH_up", "BOS_up") if want == "long" else ("CHoCH_down", "BOS_down")
        if ev.empty or not ev["kind"].isin(shift).any():
            return none
        pdd = premium_discount(visible(dpd["swings"], t, since=context_window_start(t)), price)
        if pdd is None or (want == "long" and pdd["zone"] != "discount") \
                or (want == "short" and pdd["zone"] != "premium"):
            return none
        sw = visible(d15["swings"], t, since=context_window_start(t))
        sw = sw[sw["kind"] == ("low" if want == "long" else "high")]
        if sw.empty:
            return none
        buf = RULE_SL_BUFFER_PIPS * PIP
        sl = sw.iloc[-1]["level"] - buf if want == "long" else sw.iloc[-1]["level"] + buf
        risk = (price - sl) if want == "long" else (sl - price)
        if risk < BRK_MIN_STOP_PIPS * PIP:
            return none
        pdh = pdhl(t)
        if pdh is None:
            return none
        tp = pdh[0] if want == "long" else pdh[1]            # previous-day liquidity target
        reward = (tp - price) if want == "long" else (price - tp)
        if reward < risk:
            return none
        plan = TradePlan(want, entry=price, stop_loss=sl, take_profit=tp,
                         confidence=100, reasoning="breakout+bias → PDH/PDL liquidity")
        ok, reason = validate_plan(plan, price)
        if not ok:
            return {**none, "status": "validator_rejected", "reject_reason": reason}
        return {"plan": plan, "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
    return decide


# --- deterministic mock LLM for the smoke test --------------------------------------
class MockLLM:
    """Scripted generate_fn. Cycles through: valid long / none / low-confidence long
    (gated) / malformed text (forces a retry) / valid short / R:R violation."""
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        m = re.search(r"CURRENT PRICE : (\d+\.\d+)", prompt)
        p = float(m.group(1)) if m else 1.1000
        rk = 0.0010                       # stop distance
        tgt = round(RR_TARGET * rk, 5)    # on-target TP distance (tracks BOT_RR override)
        bad = round((RR_TARGET + 1.0) * rk, 5)   # off-target distance → must fail validator
        i = self.calls % 6
        self.calls += 1
        if i == 0:
            return json.dumps({"direction": "long", "entry": p, "stop_loss": p - rk,
                               "take_profit": p + tgt, "confidence": 85,
                               "reasoning": "mock A+ long"})
        if i == 1:
            return json.dumps({"direction": "none", "entry": 0, "stop_loss": 0,
                               "take_profit": 0, "confidence": 10, "reasoning": "mock no-trade"})
        if i == 2:
            return json.dumps({"direction": "long", "entry": p, "stop_loss": p - rk,
                               "take_profit": p + tgt, "confidence": 40,
                               "reasoning": "mock low-confidence (should be gated)"})
        if i == 3:
            return "I think we should buy here because momentum looks good."   # parse failure
        if i == 4:
            return json.dumps({"direction": "short", "entry": p, "stop_loss": p + rk,
                               "take_profit": p - tgt, "confidence": 90,
                               "reasoning": "mock A+ short"})
        return json.dumps({"direction": "long", "entry": p, "stop_loss": p - rk,
                           "take_profit": p + bad, "confidence": 95,
                           "reasoning": "mock wrong-R:R (should be rejected)"})


# --- buy-and-hold reference ----------------------------------------------------------
def buy_and_hold(tf_store: dict, start: pd.Timestamp, end: pd.Timestamp,
                 equity0: float = ACCOUNT_EQUITY) -> dict:
    """Hold long EUR/USD at 1× equity notional across the window (mark-to-market on
    15m closes; one round-trip spread charged). FX has no drift entitlement — this is
    a reference line, not a strategy."""
    dec = tf_store[DECISION_TF]
    sel = dec[(dec["close_time"] > start) & (dec["close_time"] <= end)]
    entry = float(sel["open"].iloc[0])
    units = equity0 / entry
    cost = SPREAD_PIPS * PIP * units
    eq = equity0 + (sel["close"] - entry) * units - cost
    curve = pd.DataFrame({"equity": eq.values}, index=pd.DatetimeIndex(sel["close_time"]))
    curve.index.name = "time"
    trades = pd.DataFrame([{
        "strategy": "buy_hold", "direction": "long", "entry_time": sel.index[0],
        "entry": entry, "stop_loss": np.nan, "take_profit": np.nan, "units": units,
        "risk_usd": np.nan, "confidence": np.nan, "reasoning": "hold",
        "decided_at": sel.index[0], "exit_time": sel.index[-1],
        "exit_px": float(sel["close"].iloc[-1]), "reason": "end_of_run",
        "pnl_usd": float(eq.iloc[-1]) - equity0, "r_multiple": np.nan,
        "equity_after": float(eq.iloc[-1])}])
    return {"label": "buy_hold", "trades": trades, "curve": curve, "counters": {},
            "halts": [], "final_equity": float(eq.iloc[-1]),
            "final_peak": float(eq.max()), "halted": False}

print("strategies ready")

# %% [markdown]
# ## 14 · Walk-forward backtest — anchored expanding window
#
# **The split.** The test period is cut into calendar-month folds. For fold *k*, the
# *anchor* window is everything from the start of the data store up to the fold's first
# bar — detectors, structure state and LLM context for any decision inside fold *k* may
# draw only on that (always-growing) history; the fold itself is evaluated strictly
# out-of-sample, then the window rolls forward one month.
#
# **Honest caveat:** nothing here *fits* parameters — the LLM is frozen and the rules
# are fixed — so the anchored walk-forward reduces to sequential out-of-sample
# evaluation. We keep the fold structure anyway: it mirrors the standard protocol,
# gives per-fold stability diagnostics, and equity/breaker state (including the
# peak-drawdown high-water mark) carries across folds like one continuous account.
# Point-in-time discipline inside each fold is enforced by the context builder's
# `confirmed_time`/`close_time` filters and runtime leakage guard, as documented above.

# %%
def month_folds(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    edges = [start] + list(pd.date_range(start, end, freq=WF_FOLD_FREQ, inclusive="right")) + [end]
    edges = sorted(set(edges))
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if a < b]


def run_walk_forward(df_1m: pd.DataFrame, tf_store: dict, det_store: dict,
                     decide_fn: Callable, label: str,
                     start: pd.Timestamp = BACKTEST_START, end: pd.Timestamp = BACKTEST_END,
                     max_decisions: Optional[int] = None, quiet: bool = False) -> dict:
    folds = month_folds(start, end)
    carry, peak = ACCOUNT_EQUITY, ACCOUNT_EQUITY
    parts, fold_rows = [], []
    remaining = max_decisions
    # BOT_PROGRESS=1 shows a tqdm bar and writes a flushed per-fold line to
    # BOT_PROGRESS_FILE (default runs/logs/progress.txt) — readable live during a run
    # that nbconvert would otherwise buffer until the cell finishes.
    _prog = bool(os.environ.get("BOT_PROGRESS"))
    _pfile = os.environ.get("BOT_PROGRESS_FILE", str(LOG_DIR / "progress.txt"))
    _iter = enumerate(folds, 1)
    if _prog:
        import sys
        from tqdm import tqdm
        open(_pfile, "w").close()                       # reset for this run
        _iter = tqdm(_iter, total=len(folds), desc=f"{label}", file=sys.stdout, ncols=80)
    for k, (s, e) in _iter:
        r = run_engine(df_1m, tf_store, det_store, decide_fn, s, e, label,
                       equity0=carry, peak0=peak, max_decisions=remaining)
        parts.append(r)
        fold_rows.append({"fold": k, "test_start": s.date(), "test_end": e.date(),
                          "anchor": f"{df_1m.index[0].date()} → {s.date()}",
                          "trades": len(r["trades"]), "end_equity": round(r["final_equity"], 2),
                          "halted": r["halted"]})
        if remaining is not None:
            remaining = max(0, remaining - r["counters"]["llm_consults"])
        carry, peak = r["final_equity"], r["final_peak"]
        if _prog:
            with open(_pfile, "a") as _pf:
                _pf.write(f"fold {k}/{len(folds)} {100*k//len(folds)}%  {e.date()}  "
                          f"trades {len(r['trades'])}  equity ${carry:,.0f}"
                          + ("  HALTED" if r["halted"] else "") + "\n")
        elif not quiet:
            print(f"  fold {k:>2}/{len(folds)}  {s.date()} → {e.date()}  "
                  f"trades {len(r['trades']):>3}  equity ${carry:,.0f}"
                  + ("  [HALTED]" if r["halted"] else ""))
        if r["halted"]:
            print(f"  max-drawdown halt tripped — run stops here, per circuit-breaker policy")
            break
    trades = pd.concat([p["trades"] for p in parts if len(p["trades"])], ignore_index=True) \
        if any(len(p["trades"]) for p in parts) else pd.DataFrame()
    curve = pd.concat([p["curve"] for p in parts])
    counters = {}
    for p in parts:
        for k2, v in p["counters"].items():
            counters[k2] = counters.get(k2, 0) + v
    halts = [h for p in parts for h in p["halts"]]
    return {"label": label, "trades": trades, "curve": curve, "counters": counters,
            "halts": halts, "final_equity": carry, "final_peak": peak,
            "halted": parts[-1]["halted"], "folds": pd.DataFrame(fold_rows)}

print(f"walk-forward ready — folds: {len(month_folds(BACKTEST_START, BACKTEST_END))} "
      f"({BACKTEST_START.date()} → {BACKTEST_END.date()})")

# %% [markdown]
# ## 15 · Smoke test — synthetic data, mock LLM, full pipeline
#
# Runs unconditionally and fast (no Ollama, no Parquet store needed): synthetic 1m
# data → clean → resample → detectors → context builder → mock LLM → validator →
# cache/log → simulator → assertions on every code path (fills, confidence gate,
# retry, validator rejection, cache replay).

# %%
_t0 = _time.time()
smoke_raw = make_synthetic_1m(days=15, seed=11)
smoke_1m, _ = drop_weekends(smoke_raw)
sm_tf, sm_det = prepare_market(smoke_1m)
# The assertions below exercise cold-start paths (retry, gate, parse); a smoke cache
# left over from a previous run would replay everything and mask them — start cold.
shutil.rmtree(CACHE_DIR / "smoke", ignore_errors=True)
sm_cache = LLMCache(CACHE_DIR / "smoke")
sm_logger = JsonlLogger(LOG_DIR / f"smoke_{RUN_ID}.jsonl")
sm_decide = make_llm_decide_fn(sm_cache, sm_logger, generate_fn=MockLLM())

sm_start, sm_end = smoke_1m.index[0], smoke_1m.index[-1] + pd.Timedelta("1min")
sm_res = run_engine(smoke_1m, sm_tf, sm_det, sm_decide, sm_start, sm_end, "mock_llm")

cnt = sm_res["counters"]
assert cnt["fills"] >= 1, "smoke: expected at least one filled trade"
assert cnt["gated"] >= 1, "smoke: confidence gate never triggered"
assert cnt["retries"] >= 1, "smoke: retry path never exercised"
assert cnt["no_trade"] >= 1, "smoke: 'none' path never exercised"
assert len(sm_res["curve"]) > 100 and np.isfinite(sm_res["final_equity"])
assert not sm_res["trades"].empty and {"r_multiple", "reason"} <= set(sm_res["trades"].columns)

# cache replay: identical contexts must be answered from cache, zero mock calls
sm_decide2 = make_llm_decide_fn(sm_cache, sm_logger, generate_fn=MockLLM())
sm_res2 = run_engine(smoke_1m, sm_tf, sm_det, sm_decide2, sm_start, sm_end, "mock_llm_replay")
assert sm_res2["counters"]["cache_hits"] == sm_res2["counters"]["llm_consults"], \
    "smoke: cache replay should answer every consult"

# rule baseline runs end-to-end on synthetic too (trade count may legitimately be 0)
sm_rule = run_engine(smoke_1m, sm_tf, sm_det, make_rule_decide_fn(sm_det),
                     sm_start, sm_end, "rule_smoke")

print(f"SMOKE OK in {_time.time() - _t0:.1f}s — consults {cnt['llm_consults']}, "
      f"fills {cnt['fills']}, gated {cnt['gated']}, retries {cnt['retries']}, "
      f"rejected {cnt['validator_rejected']}, cache replay clean, "
      f"rule-baseline trades {len(sm_rule['trades'])}")
# %% [markdown]
# ## 16 · Real-data runs
#
# Guards: the LLM walk-forward needs the real Parquet store **and** a ready LLM
# provider (Gemini key or running Ollama, per `LLM_PROVIDER`); the rule baseline and
# buy-and-hold need only the store. If something is missing the notebook still
# completes — the reporting cells fall back to the smoke results so every artifact
# below always renders.

# %%
def ollama_alive() -> bool:
    try:
        r = requests.get(OLLAMA_URL.replace("/api/generate", "/api/tags"), timeout=3)
        return r.ok and any(m.get("name", "").startswith(OLLAMA_MODEL.split(":")[0])
                            for m in r.json().get("models", []))
    except requests.RequestException:
        return False


def llm_ready() -> bool:
    """Provider-aware healthcheck: one real (cheap) generation must succeed."""
    if LLM_PROVIDER == "ollama":
        return ollama_alive()
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is not set — export it (aistudio.google.com/apikey) "
              "or set LLM_PROVIDER='ollama' in the config cell")
        return False
    try:
        gemini_generate('Respond with exactly this JSON: {"ok": true}', system="echo test")
        return True
    except Exception as e:
        print(f"Gemini healthcheck failed: {e!r}")
        return False

SKIP_LLM = bool(os.environ.get("BOT_SKIP_LLM"))   # rule-baseline-only run
LLM_READY = (not SKIP_LLM) and llm_ready()
DO_REAL = HAVE_REAL_DATA and not RUN_SMOKE_ONLY
DO_LLM = DO_REAL and LLM_READY
print(f"real data: {HAVE_REAL_DATA} | {LLM_MODEL_ID} ready: {LLM_READY} "
      f"| real runs: {DO_REAL} | LLM run: {DO_LLM}")

# %%
RESULTS: dict[str, dict] = {}

if DO_REAL:
    _t0 = _time.time()
    INTRADAY_START = BACKTEST_START - pd.Timedelta(days=60)   # detector warm-up buffer
    M_TF, M_DET = prepare_market(clean_1m, intraday_start=INTRADAY_START)
    _dec_ct = pd.DatetimeIndex(M_TF[DECISION_TF]["close_time"])
    _slots = int(((_dec_ct > BACKTEST_START) & (_dec_ct <= BACKTEST_END)
                  & in_ny_session(_dec_ct)).sum())
    _per_call = max(1.5, GEMINI_MIN_INTERVAL_S) if LLM_PROVIDER == "gemini" else 40
    print(f"market prepared in {_time.time() - _t0:.0f}s — {_slots:,} NY-session decision "
          f"slots in window (≈{_slots * _per_call / 3600:.1f}h of LLM compute at "
          f"~{_per_call:.0f}s/call via {LLM_MODEL_ID}, fewer while a position is open)")

# %% [markdown]
# ### Rule-only ICT baseline + buy-and-hold (deterministic, fast)

# %%
if DO_REAL and os.environ.get("BOT_COMPARE_STRATS"):
    # Three named ICT strategies compared head-to-head (see STRATEGY_COMPARISON_SPEC.md).
    # SLIPSTREAM is keyed "rule_ict" so the existing report wiring works unchanged.
    print("3-strategy ICT comparison — SLIPSTREAM / MIDNIGHT RAID / BLOODHOUND:")
    RESULTS["rule_ict"] = run_walk_forward(clean_1m, M_TF, M_DET,
        make_breakout_decide_fn(M_DET), "SLIPSTREAM")
    RESULTS["midnight_raid"] = run_walk_forward(clean_1m, M_TF, M_DET,
        make_judas_decide_fn(M_DET, M_TF), "MIDNIGHT RAID")
    RESULTS["bloodhound"] = run_walk_forward(clean_1m, M_TF, M_DET,
        make_breakout_liq_decide_fn(M_DET, M_TF), "BLOODHOUND")
    for _k in ("rule_ict", "midnight_raid", "bloodhound"):
        _r = RESULTS[_k]
        print(f"  {_r['label']:14} trades {len(_r['trades']):>3}  "
              f"final ${_r['final_equity']:,.0f}")
    RESULTS["buy_hold"] = buy_and_hold(M_TF, BACKTEST_START, BACKTEST_END)
    print(f"buy-and-hold final equity: ${RESULTS['buy_hold']['final_equity']:,.0f}")
elif DO_REAL:
    # BOT_STRATEGY=breakout_bias swaps the shipped reversal rule for the improved
    # breakout + 1h-HTF-bias strategy (Test 5 winner). Kept under the "rule_ict" key
    # so all downstream reporting works unchanged; the label reflects which ran.
    _strat = os.environ.get("BOT_STRATEGY", "reversal")
    if _strat == "breakout_bias":
        _decide, _label = make_breakout_decide_fn(M_DET), "ict_breakout_bias"
    else:
        _decide, _label = make_rule_decide_fn(M_DET), "rule_ict"
    print(f"rule strategy: {_label} (RR target {RR_TARGET:g}):")
    RESULTS["rule_ict"] = run_walk_forward(clean_1m, M_TF, M_DET, _decide, _label)
    RESULTS["buy_hold"] = buy_and_hold(M_TF, BACKTEST_START, BACKTEST_END)
    print(f"buy-and-hold final equity: ${RESULTS['buy_hold']['final_equity']:,.0f}")
else:
    print("real data unavailable / smoke-only — reporting will use smoke results")
    RESULTS["rule_ict"] = sm_rule
    RESULTS["buy_hold"] = buy_and_hold(sm_tf, sm_start, sm_end)

# %% [markdown]
# ### LLM walk-forward (the headline run)
#
# Every NY-session 15m close while flat → context → the configured model
# (`LLM_MODEL_ID`) → validated plan. Responses are cached by context hash, so
# re-running the notebook replays from disk — widening the window later reuses
# every completed call. Audit trail: `runs/logs/llm_calls_<run-id>.jsonl`.

# %%
if DO_LLM:
    llm_cache = LLMCache(CACHE_DIR / "real")
    llm_logger = JsonlLogger(LOG_DIR / f"llm_calls_{RUN_ID}.jsonl")
    print(f"LLM walk-forward ({LLM_MODEL_ID}, temp 0, seed {ACTIVE_LLM_PARAMS.get('seed')}):")
    _t0 = _time.time()
    RESULTS["llm_ict"] = run_walk_forward(
        clean_1m, M_TF, M_DET, make_llm_decide_fn(llm_cache, llm_logger), "llm_ict",
        max_decisions=MAX_LLM_DECISIONS)
    _c = RESULTS["llm_ict"]["counters"]
    print(f"done in {(_time.time() - _t0) / 3600:.2f}h — consults {_c['llm_consults']:,} "
          f"(cache {_c['cache_hits']:,}), orders {_c['orders']}, gated {_c['gated']}, "
          f"rejected {_c['validator_rejected']}, llm errors {_c['llm_errors']}")
elif DO_REAL:
    print("LLM walk-forward skipped "
          + ("(BOT_SKIP_LLM set — rule baseline + buy-and-hold only)." if SKIP_LLM else
             f"({LLM_MODEL_ID} not ready. For Gemini: export GEMINI_API_KEY. "
             "For Ollama: start `ollama serve` and pull the model)."))
else:
    RESULTS["llm_ict"] = sm_res          # smoke stand-in so reporting always renders

# %% [markdown]
# ### Live-forward mode
#
# The same engine driven in strict arrival order over the most recent days — a paper
# replay of real-time decisioning (`verbose=True` streams every order, fill and exit
# as it would print live; no internet at runtime, so "live" means replaying the newest
# bars in the store). A short rule-strategy demo always runs; set
# `RUN_MODE = "live_forward"` to run the LLM live-forward over `LIVE_FORWARD_DAYS`.

# %%
if DO_REAL:
    lf_end = BACKTEST_END
    lf_start = lf_end - pd.Timedelta(days=3)
    print(f"live-forward demo (rule strategy, {lf_start.date()} → {lf_end.date()}):")
    lf_demo = run_engine(clean_1m, M_TF, M_DET, make_rule_decide_fn(M_DET),
                         lf_start, lf_end, "rule_live_demo", verbose=True)
    print(f"demo finished — {len(lf_demo['trades'])} trade(s), "
          f"final equity ${lf_demo['final_equity']:,.0f}")
    if RUN_MODE == "live_forward" and LLM_READY:
        lf_start = lf_end - pd.Timedelta(days=LIVE_FORWARD_DAYS)
        print(f"\nLIVE-FORWARD LLM run ({lf_start.date()} → {lf_end.date()}):")
        RESULTS["llm_live"] = run_engine(
            clean_1m, M_TF, M_DET,
            make_llm_decide_fn(LLMCache(CACHE_DIR / "real"),
                               JsonlLogger(LOG_DIR / f"llm_live_{RUN_ID}.jsonl")),
            lf_start, lf_end, "llm_live", verbose=True)

# %% [markdown]
# ## 17 · Metrics
#
# Win rate, average R, Sharpe, Sortino, max drawdown, profit factor, expectancy,
# trade count and average hold time — computed identically for every strategy.
# Sharpe/Sortino use daily equity returns annualized by √252.

# %%
def compute_metrics(res: dict, equity0: float = ACCOUNT_EQUITY) -> dict:
    tr, eq = res["trades"], res["curve"]["equity"]
    out = {"strategy": res["label"],
           "final_equity": round(res["final_equity"], 2),
           "total_return_pct": round(100 * (res["final_equity"] / equity0 - 1), 2)}
    closed = tr[tr["r_multiple"].notna()] if "r_multiple" in tr.columns and len(tr) else pd.DataFrame()
    if len(closed):
        pnl, r = closed["pnl_usd"], closed["r_multiple"]
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        hold = (pd.to_datetime(closed["exit_time"]) - pd.to_datetime(closed["entry_time"]))
        out.update(trades=len(closed),
                   win_rate_pct=round(100 * (pnl > 0).mean(), 1),
                   avg_R=round(r.mean(), 3),
                   expectancy_usd=round(pnl.mean(), 2),
                   profit_factor=round(wins.sum() / abs(losses.sum()), 2) if len(losses) and losses.sum() != 0 else float("inf"),
                   avg_hold_hours=round(hold.dt.total_seconds().mean() / 3600, 2))
    else:
        out.update(trades=0, win_rate_pct=np.nan, avg_R=np.nan, expectancy_usd=np.nan,
                   profit_factor=np.nan, avg_hold_hours=np.nan)
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) > 2 and rets.std() > 0:
        out["sharpe"] = round(rets.mean() / rets.std() * np.sqrt(252), 2)
        downside = rets[rets < 0].std()
        out["sortino"] = round(rets.mean() / downside * np.sqrt(252), 2) \
            if downside and downside > 0 else float("inf")
    else:
        out["sharpe"] = out["sortino"] = np.nan
    out["max_drawdown_pct"] = round(100 * (eq / eq.cummax() - 1).min(), 2)
    return out


summary_df = pd.DataFrame([compute_metrics(r) for r in RESULTS.values()]).set_index("strategy")
summary_df
# %% [markdown]
# ## 18 · Plots — equity, drawdown, trade markers

# %%
import base64
import matplotlib
import matplotlib.pyplot as plt
from jinja2 import Template

PLOT_PATHS = {}

def _save(fig, name: str) -> None:
    p = REPORT_DIR / f"{name}_{RUN_ID}.png"
    fig.savefig(p, dpi=110, bbox_inches="tight")
    PLOT_PATHS[name] = p

fig, ax = plt.subplots(figsize=(11, 4.5))
for key, res in RESULTS.items():
    ax.plot(res["curve"].index, res["curve"]["equity"], label=f"{res['label']}", lw=1.2)
# S&P 500 benchmark — daily closes fetched once into the data store (no internet at
# runtime), scaled to starting equity over the same window.
_spx_csv = DATA_PATH / "spx_daily.csv"
if _spx_csv.exists() and RESULTS:
    _w0 = min(r["curve"].index.min() for r in RESULTS.values())
    _w1 = max(r["curve"].index.max() for r in RESULTS.values())
    _spx = pd.read_csv(_spx_csv, parse_dates=["date"]).set_index("date")["close"]
    _spx = _spx[(_spx.index >= _w0.normalize()) & (_spx.index <= _w1)]
    if len(_spx) > 1:
        ax.plot(_spx.index, _spx / _spx.iloc[0] * ACCOUNT_EQUITY,
                lw=1.1, ls="--", color="dimgray", label="S&P 500 (scaled)")
ax.axhline(ACCOUNT_EQUITY, color="grey", lw=0.7, ls="--")
ax.set_title("Equity curves"); ax.set_ylabel("USD"); ax.grid(alpha=0.3)
ax.legend(loc="center left", bbox_to_anchor=(1.005, 0.5))   # outside, never on the data
_save(fig, "equity"); plt.show()

fig, ax = plt.subplots(figsize=(11, 3.2))
for key, res in RESULTS.items():
    eq = res["curve"]["equity"]
    dd = 100 * (eq / eq.cummax() - 1)
    ax.plot(dd.index, dd, label=res["label"], lw=1.0)
ax.axhline(-100 * MAX_DRAWDOWN_HALT, color="red", lw=0.8, ls=":", label="halt level")
ax.set_title("Drawdown (%)"); ax.set_ylabel("%"); ax.grid(alpha=0.3)
ax.legend(loc="center left", bbox_to_anchor=(1.005, 0.5))
_save(fig, "drawdown"); plt.show()

# trade markers on price for the strategy with the most closed trades
_mk = max(RESULTS.values(),
          key=lambda r: len(r["trades"]) if "r_multiple" in getattr(r["trades"], "columns", []) else 0)
_tr = _mk["trades"]
_px_tf = (M_TF if DO_REAL else sm_tf)[DECISION_TF]
_w0, _w1 = _mk["curve"].index.min(), _mk["curve"].index.max()
_px = _px_tf[(_px_tf["close_time"] >= _w0) & (_px_tf["close_time"] <= _w1)]
fig, ax = plt.subplots(figsize=(11, 4.5))
ax.plot(_px.index, _px["close"], lw=0.6, color="black", alpha=0.7, label="EUR/USD 15m close")
if len(_tr) and "r_multiple" in _tr.columns:
    closed = _tr[_tr["r_multiple"].notna()]
    longs, shorts = closed[closed["direction"] == "long"], closed[closed["direction"] == "short"]
    ax.scatter(longs["entry_time"], longs["entry"], marker="^", color="green", s=42, label="long entry", zorder=3)
    ax.scatter(shorts["entry_time"], shorts["entry"], marker="v", color="red", s=42, label="short entry", zorder=3)
    win = closed[closed["pnl_usd"] > 0]; loss = closed[closed["pnl_usd"] <= 0]
    ax.scatter(win["exit_time"], win["exit_px"], marker="o", facecolors="none", edgecolors="green", s=36, label="exit (win)", zorder=3)
    ax.scatter(loss["exit_time"], loss["exit_px"], marker="x", color="red", s=36, label="exit (loss)", zorder=3)
ax.set_title(f"Trades on price — {_mk['label']}"); ax.grid(alpha=0.3)
ax.legend(loc="center left", bbox_to_anchor=(1.005, 0.5))
_save(fig, "trades"); plt.show()

# %% [markdown]
# ## 19 · HTML report + CSV trade logs

# %%
def build_verdict() -> str:
    if "llm_ict" not in RESULTS:
        rule = compute_metrics(RESULTS["rule_ict"])
        return (f"LLM layer deliberately skipped for this run (BOT_SKIP_LLM) — mechanical "
                f"rule baseline and buy-and-hold only. Rule baseline: "
                f"{rule['total_return_pct']:+.2f}% over {rule['trades']} trades.")
    llm = compute_metrics(RESULTS["llm_ict"])
    rule = compute_metrics(RESULTS["rule_ict"])
    llm_real = DO_LLM and RESULTS["llm_ict"]["label"] == "llm_ict"
    if not llm_real:
        return ("This report was generated from the smoke-test stand-in (no real-data LLM run "
                "was executed), so no LLM-vs-rules conclusion can be drawn from it.")
    beat = (llm["total_return_pct"] > rule["total_return_pct"]) and \
           (np.isnan(rule.get("sharpe", np.nan)) or
            (not np.isnan(llm.get("sharpe", np.nan)) and llm["sharpe"] >= rule["sharpe"]))
    if beat:
        return (f"On the same out-of-sample window the LLM strategy returned "
                f"{llm['total_return_pct']:+.2f}% (Sharpe {llm['sharpe']}) vs the mechanical "
                f"rule baseline's {rule['total_return_pct']:+.2f}% (Sharpe {rule['sharpe']}) — "
                f"the LLM added value over its own deterministic inputs on this window. "
                f"Treat with caution: one window, one pair, paper costs.")
    return (f"**The LLM did not beat the mechanical rules.** On identical out-of-sample data the "
            f"LLM strategy returned {llm['total_return_pct']:+.2f}% (Sharpe {llm['sharpe']}, "
            f"avg R {llm['avg_R']}) vs {rule['total_return_pct']:+.2f}% (Sharpe {rule['sharpe']}, "
            f"avg R {rule['avg_R']}) for the rule-only ICT baseline. On this evidence the LLM "
            f"layer adds cost and latency without adding edge.")

VERDICT = build_verdict()

_REPORT_TMPL = Template("""<!doctype html><html><head><meta charset="utf-8">
<title>LLM + ICT paper bot — {{ run_id }}</title>
<style>
 body{font-family:-apple-system,Segoe UI,sans-serif;margin:2.2em;max-width:1100px;color:#1a1a1a}
 h1{font-size:1.5em}h2{font-size:1.15em;margin-top:1.6em;border-bottom:1px solid #ddd;padding-bottom:.2em}
 table{border-collapse:collapse;font-size:.85em;margin:.6em 0}
 th,td{border:1px solid #ccc;padding:.3em .6em;text-align:right}
 th{background:#f2f2f2}td:first-child,th:first-child{text-align:left}
 .verdict{background:#f8f4e8;border-left:4px solid #c9a227;padding:.9em 1.1em;margin:1em 0;font-size:.95em}
 .meta{color:#555;font-size:.85em}img{max-width:100%;border:1px solid #eee;margin:.4em 0}
</style></head><body>
<h1>LLM + ICT hybrid paper-trading bot — EUR/USD</h1>
<p class="meta">run {{ run_id }} · mode {{ mode }} · window {{ start }} → {{ end }} ·
model {{ model }} (temp 0, seed {{ seed }}) · decision TF {{ dec_tf }} · NY session entries only ·
spread {{ spread }} pip · risk {{ risk }}%/trade · RR 1:{{ rr }} · confidence gate ≥ {{ gate }}</p>
<div class="verdict"><b>Robustness verdict.</b> {{ verdict }}</div>
<h2>Summary — LLM vs rule-only vs buy-and-hold</h2>
{{ summary_table }}
<h2>Decision funnel & skip counts</h2>
{{ counters_table }}
<h2>Circuit-breaker halts</h2>
{% if halts %}<table><tr><th>strategy</th><th>time</th><th>kind</th><th>detail</th></tr>
{% for h in halts %}<tr><td>{{ h.strategy }}</td><td>{{ h.time }}</td><td>{{ h.kind }}</td><td>{{ h.detail }}</td></tr>{% endfor %}
</table>{% else %}<p>None tripped.</p>{% endif %}
{% if folds_table %}<h2>Walk-forward folds (anchored expanding window)</h2>{{ folds_table }}{% endif %}
<h2>Equity curves</h2><img src="data:image/png;base64,{{ img_equity }}">
<h2>Drawdown</h2><img src="data:image/png;base64,{{ img_drawdown }}">
<h2>Trades on price</h2><img src="data:image/png;base64,{{ img_trades }}">
<h2>Data quality</h2>
<p class="meta">{{ data_note }}</p>
</body></html>""")

def _b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()

counters_df = pd.DataFrame({res["label"]: res["counters"] for res in RESULTS.values()
                            if res["counters"]}).fillna(0).astype(int)
halts_flat = [{"strategy": res["label"], **h} for res in RESULTS.values() for h in res["halts"]]
_ref = RESULTS.get("llm_ict", RESULTS["rule_ict"])    # window/fold reference strategy
folds_html = _ref["folds"].to_html(index=False) if "folds" in _ref else None

html = _REPORT_TMPL.render(
    run_id=RUN_ID, mode=RUN_MODE,
    start=str(_ref["curve"].index.min().date()),
    end=str(_ref["curve"].index.max().date()),
    model=LLM_MODEL_ID, seed=ACTIVE_LLM_PARAMS.get("seed"), dec_tf=DECISION_TF,
    spread=SPREAD_PIPS, risk=int(RISK_PCT * 100), rr=int(RR_TARGET), gate=CONF_THRESHOLD,
    verdict=VERDICT,
    summary_table=summary_df.to_html(),
    counters_table=counters_df.to_html(),
    halts=halts_flat, folds_table=folds_html,
    img_equity=_b64(PLOT_PATHS["equity"]), img_drawdown=_b64(PLOT_PATHS["drawdown"]),
    img_trades=_b64(PLOT_PATHS["trades"]),
    data_note=(f"source: {'real parquet store' if HAVE_REAL_DATA else 'synthetic fallback'} · "
               f"{len(clean_1m):,} clean 1m bars · {n_weekend:,} weekend bars dropped · "
               f"{len(holiday_days)} holiday days dropped · {len(gaps_df):,} intra-session "
               f"gaps flagged (never filled) · {len(thin_days)} thin days flagged"))

report_path = REPORT_DIR / f"report_{RUN_ID}.html"
report_path.write_text(html)

csv_paths = []
for res in RESULTS.values():
    if len(res["trades"]):
        p = REPORT_DIR / f"trades_{res['label']}_{RUN_ID}.csv"
        res["trades"].to_csv(p, index=False)
        csv_paths.append(p)

print(f"report  → {report_path}")
for p in csv_paths:
    print(f"trades  → {p}")

# %% [markdown]
# ## 20 · Final summary

# %%
print("=" * 78)
print("LLM + ICT HYBRID PAPER BOT — RUN SUMMARY")
print("=" * 78)
print(f"mode {RUN_MODE} | window {BACKTEST_START.date()} → {BACKTEST_END.date()} | "
      f"model {LLM_MODEL_ID} | smoke {'PASS' if cnt['fills'] >= 1 else '??'}")
print("-" * 78)
print(summary_df.to_string())
print("-" * 78)
for res in RESULTS.values():
    c = res["counters"]
    if c:
        print(f"{res['label']:>10}: consults {c.get('llm_consults', 0):,} | "
              f"no-trade {c.get('no_trade', 0):,} | gated {c.get('gated', 0):,} | "
              f"rejected {c.get('validator_rejected', 0):,} | fills {c.get('fills', 0):,} | "
              f"breaker blocks {c.get('breaker_day_blocks', 0) + c.get('breaker_pause_blocks', 0) + c.get('breaker_halt_blocks', 0):,}")
print("-" * 78)
print("VERDICT:", re.sub(r"\*\*", "", VERDICT))
print(f"artifacts: {report_path.name}, JSONL audit logs in {LOG_DIR}/, "
      f"LLM cache in {CACHE_DIR}/")
print("=" * 78)
