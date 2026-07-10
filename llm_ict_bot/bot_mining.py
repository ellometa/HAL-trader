"""Test 16-prep — bot mining: the best transferable strategies from open-source bots.

Surveyed: freqtrade-strategies (hyperopt-fit TA mashups backtested on 20 days of
2018 — nothing to take beyond the protections already ported in Test 10),
jesse example-strategies (same family), je-suis-tm/quant-trading (17 strategies,
one genuine transferable), QuantConnect community library (academic replications —
the good stuff). Two candidates survived the evidence filter:

GEM — Antonacci Global Equities Momentum (the most-implemented strategy among
      tactical-allocation bots; book + decades of published evidence).
      Monthly: if 12-mo SPY return > 12-mo EFA return and > 12-mo T-bill (BIL)
      return → hold SPY; if EFA wins and beats BIL → hold EFA; else hold AGG.
      ~1-2 switches/yr. Value to us: MOMENTUM — structurally uncorrelated with
      FORTRESS's mean-reversion/seasonality legs.

DUAL THRUST — classic opening-range breakout (Michael Chalek; futures-desk legacy;
      je-suis-tm implementation). Range = max(HH-LC, HC-LL) of prior 4 days;
      long when price breaks day-open + 0.5×Range. Long-only here (spot). Tested
      on BTC 15m (the only intraday data in repo until gold lands).

Run:  .venv/bin/python llm_ict_bot/bot_mining.py   (from the HAL repo root)
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pivot_screen import COST, ROOT, load_btc, load_equity, run, spy_portfolio
from stress_funded import PPY, ann
from eval_lab import tlt_sleeve

def load(sym: str) -> pd.Series:
    return load_equity(sym)["close"]

# --- GEM (Antonacci dual momentum) ------------------------------------------------------
def gem() -> tuple[pd.Series, pd.Series]:
    """Monthly dual momentum on SPY/EFA/AGG with BIL absolute-momentum filter.
    Returns (daily strategy returns, holdings series)."""
    px = pd.DataFrame({s: load(s) for s in ("SPY", "EFA", "AGG", "BIL")}).dropna()
    m = px.resample("ME").last()
    r12 = m.pct_change(12)                                   # 12-month lookback
    eq_win = np.where(r12["SPY"] >= r12["EFA"], "SPY", "EFA")
    eq_ret = np.where(r12["SPY"] >= r12["EFA"], r12["SPY"], r12["EFA"])
    hold = pd.Series(np.where(eq_ret > r12["BIL"], eq_win, "AGG"), index=m.index)
    hold = hold[~(r12["SPY"].isna() | r12["BIL"].isna())]    # drop warm-up months
    # decided at month close -> held the following month (no lookahead)
    daily_hold = hold.shift(1).dropna().reindex(px.index, method="ffill").dropna()
    ret = px.pct_change()
    out = pd.Series(0.0, index=daily_hold.index)
    for s in ("SPY", "EFA", "AGG"):
        out[daily_hold == s] = ret[s].reindex(daily_hold.index)[daily_hold == s]
    switch = daily_hold != daily_hold.shift()
    switch.iloc[0] = False
    out -= switch * 2 * COST["SPY"]                          # sell+buy on switch
    return out.fillna(0.0), daily_hold

# --- Dual Thrust on BTC 15m --------------------------------------------------------------
def dual_thrust_btc(k1: float = 0.5, n: int = 4) -> pd.Series:
    """Long when price breaks UTC-day open + k1 × Range, flat at day end.
    Range = max(HH-LC, HC-LL) over the prior n days. Long-only (spot)."""
    h1, d1 = load_btc()
    f15 = pd.read_excel(ROOT / "data/BTCUSDT_15m.xlsx").set_index("Datetime")
    f15.columns = [c.lower() for c in f15.columns]
    hh = d1["high"].rolling(n).max().shift(1)
    ll = d1["low"].rolling(n).min().shift(1)
    hc = d1["close"].rolling(n).max().shift(1)
    lc = d1["close"].rolling(n).min().shift(1)
    rng = pd.concat([hh - lc, hc - ll], axis=1).max(axis=1)
    day = pd.Series(f15.index.normalize(), index=f15.index)
    day_open = f15["open"].groupby(day).transform("first")
    trigger = day_open + k1 * rng.reindex(day.to_numpy()).to_numpy()
    # signal on the 15m close crossing the trigger; held to day end (flat overnight)
    brk = (f15["close"] > trigger)
    pos = brk.groupby(day).cummax().astype(float)            # once broken, hold rest of day
    last_bar = day != day.shift(-1).fillna(day.iloc[-1])
    pos[last_bar] = 0.0                                      # flat into the close
    r = f15["close"].pct_change().shift(-1)
    net = (pos * r - (pos.diff().abs().fillna(0)) * 0.0010).fillna(0)   # 10 bps/side
    return net

def main():
    print("GEM — Antonacci dual momentum (SPY/EFA/AGG, BIL filter), 2004-2026")
    g, holds = gem()
    c, s, d = ann(g)
    n_sw = (holds != holds.shift()).sum()
    print(f"  CAGR {c*100:5.1f}%  Sharpe {s:4.2f}  MaxDD {d*100:5.1f}%  "
          f"switches {n_sw} ({n_sw/((len(g)/PPY)):.1f}/yr)")
    for a, b in (("2004", "2015"), ("2015", "2027")):
        sl = g[(g.index >= a) & (g.index < b)]
        c1, s1, d1_ = ann(sl)
        print(f"    {a}-{b}: CAGR {c1*100:5.1f}%  Sharpe {s1:4.2f}  MaxDD {d1_*100:5.1f}%")
    spy_bh = load("SPY").pct_change().reindex(g.index).fillna(0)
    cb, sb, db = ann(spy_bh)
    print(f"  SPY buy&hold same window: CAGR {cb*100:.1f}%  Sharpe {sb:.2f}  MaxDD {db*100:.0f}%")

    # correlation with FORTRESS — the reason GEM matters to us
    spy, tlt = load_equity("SPY"), load_equity("TLT")
    sp = spy_portfolio(spy, "avg")
    ts_ = tlt_sleeve(tlt)
    ix = sp["net"].index.intersection(ts_["net"].index).intersection(g.index)
    f_net = 0.7 * sp["net"].reindex(ix).fillna(0) + 0.3 * ts_["net"].reindex(ix).fillna(0)
    gm = g.reindex(ix).resample("ME").sum()
    fm = f_net.resample("ME").sum()
    print(f"  monthly-return correlation GEM vs FORTRESS: {gm.corr(fm):+.2f}")
    blend = 0.5 * g.reindex(ix).fillna(0) + 0.5 * f_net
    c2, s2, d2 = ann(blend)
    print(f"  50/50 GEM+FORTRESS blend: CAGR {c2*100:.1f}%  Sharpe {s2:.2f}  MaxDD {d2*100:.1f}%")

    print("\nDUAL THRUST — BTC 15m, long-only, k1=0.5, n=4d, 10bps/side, 2020-2026")
    dt = dual_thrust_btc()
    c, s, d = ann(dt.groupby(dt.index.normalize()).sum())    # daily-ize for metrics
    print(f"  CAGR {c*100:5.1f}%  Sharpe {s:4.2f}  MaxDD {d*100:5.1f}%")
    oos = dt[dt.index >= "2025-01-01"]
    c, s, d = ann(oos.groupby(oos.index.normalize()).sum())
    print(f"  2025+ : CAGR {c*100:5.1f}%  Sharpe {s:4.2f}  MaxDD {d*100:5.1f}%")

if __name__ == "__main__":
    main()
