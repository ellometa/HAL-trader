"""Test 14 — more data: 64-year validation + intraday-aware eval limits.

Two upgrades that close the honesty gaps flagged in Tests 10-13:

A · Deep-history validation (^GSPC 1962-2026, 16,238 days)
    The five equity legs re-run on the S&P index itself, split 1962-1992 (pre-SPY,
    pre-publication — pure out-of-sample) vs 1993-2026. An anomaly that only exists
    after 1993 is fragile; one that holds since 1962 is structural. (Index has no
    dividends → buy-hold benchmark understated; strategy *relative* numbers stand.)

B · Intraday-aware funded-account limits
    The −5% daily / −10% total lines were checked close-to-close. Daily bars contain
    the intraday LOW, so the worst intra-day mark of a long position is bounded by
    low/prev_close − 1 per instrument. The eval sim now bootstraps (day-return,
    intraday-worst) pairs jointly and kills the account if the intraday mark breaches
    either line — the conservative bound (assumes the full position rides the exact
    low of every instrument simultaneously). Truth lies between the close-only and
    intraday-aware numbers.

Run:  .venv/bin/python llm_ict_bot/more_data_check.py   (from the HAL repo root)
"""
from __future__ import annotations

import base64
import io
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pivot_screen import (COST, ROOT, double7, ibs_strategy, load_equity,
                          rsi2_plain, run, spy_end_of_month, spy_portfolio,
                          spy_rsi2_200ma, spy_turnaround_tuesday)
from stress_funded import PPY, ann, block_bootstrap
from eval_lab import tlt_sleeve

RNG = np.random.default_rng(21)

# --- A · deep history --------------------------------------------------------------
def deep_history():
    g = load_equity("GSPC")
    legs = {
        "RSI-2 + 200MA": spy_rsi2_200ma,
        "Turnaround Tuesday": spy_turnaround_tuesday,
        "End-of-month": spy_end_of_month,
        "IBS": lambda px: ibs_strategy(px, "SPY"),
        "Double 7s": lambda px: double7(px, "SPY"),
        "PORTFOLIO avg": lambda px: spy_portfolio(px, "avg"),
    }
    rows = []
    print("A · ^GSPC 1962-2026 — pre-1993 (pure OOS) vs post-1993")
    print(f"{'leg':22} {'62-92 CAGR':>10} {'62-92 Shp':>9} | {'93-26 CAGR':>10} {'93-26 Shp':>9}")
    for name, fn in legs.items():
        net = fn(g)["net"]
        pre = net[net.index < "1993-01-01"]
        post = net[net.index >= "1993-01-01"]
        (c1, s1, d1), (c2, s2, d2) = ann(pre), ann(post)
        rows.append((name, c1, s1, c2, s2))
        print(f"{name:22} {c1*100:9.1f}% {s1:9.2f} | {c2*100:9.1f}% {s2:9.2f}")
    print()
    return rows

# --- B · intraday-aware eval -------------------------------------------------------
def daily_streams(px: pd.DataFrame, pos: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """(day-return, intraday-worst) for a position entered at the prior close.
    ret_d = close_d/close_{d-1} - 1 ; worst_d = low_d/close_{d-1} - 1 (long)."""
    pc = px["close"].shift(1)
    held = pos.shift(1).fillna(0)                            # pos decided at close d-1
    ret = held * (px["close"] / pc - 1)
    worst = held * (px["low"] / pc - 1)
    return ret.fillna(0).to_numpy(), worst.fillna(0).to_numpy()

def eval_sim_intraday(ret: np.ndarray, worst: np.ndarray, pos: np.ndarray, firm: dict,
                      lev: float, n_sims: int = 3000, horizon: int = 1000) -> dict:
    """Same state machine as eval_lab.eval_sim, but breaches are checked on the
    intraday worst mark, not just the close."""
    fin = 0.06 / PPY * np.maximum(0.0, lev * pos - 1.0)
    n = len(ret)
    block, nb = 5, horizon // 5 + 1
    starts = RNG.integers(1, n - block, size=(n_sims, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_sims, -1)[:, :horizon]
    R = lev * ret[idx] - fin[idx]
    W = lev * worst[idx] - fin[idx]

    P1, P2, FUNDED, DEAD = 0, 1, 2, 3
    state = np.zeros(n_sims, dtype=int)
    bal = np.full(n_sims, 100_000.0)
    payout = np.zeros(n_sims)
    funded_day = np.full(n_sims, -1)
    dim = 0
    for d in range(horizon):
        live = state != DEAD
        w_pnl = bal * W[:, d] * live                          # worst intraday mark
        dead = live & ((w_pnl <= -5_000) | (bal + w_pnl < 90_000))
        state[dead] = DEAD
        live = state != DEAD
        pnl = bal * R[:, d] * live
        bal = bal + pnl
        dead2 = live & ((pnl <= -5_000) | (bal < 90_000))
        state[dead2] = DEAD
        p1 = (state == P1) & (bal >= 100_000 * (1 + firm["t1"]))
        state[p1] = P2; bal[p1] = 100_000.0
        p2 = (state == P2) & (bal >= 100_000 * (1 + firm["t2"]))
        state[p2] = FUNDED; bal[p2] = 100_000.0
        funded_day[p2 & (funded_day < 0)] = d
        dim += 1
        if dim == 21:
            dim = 0
            f = (state == FUNDED) & (bal > 100_000)
            payout[f] += (bal[f] - 100_000) * firm["split"]
            bal[f] = 100_000.0
    got = funded_day >= 0
    return {"pass": got.mean(),
            "med_mo": np.median(funded_day[got]) / 21 if got.any() else np.nan,
            "death": ((state == DEAD) & got).mean() / got.mean() if got.any() else np.nan,
            "ev": payout.mean() - firm["fee"],
            "monthly": payout.mean() / (horizon / 21)}

def main():
    rows_a = deep_history()

    spy, tlt = load_equity("SPY"), load_equity("TLT")
    sp = spy_portfolio(spy, "avg")
    ts_ = tlt_sleeve(tlt)
    # per-instrument (ret, worst) streams on the common index
    ix = spy.index.intersection(tlt.index)
    rs, ws = daily_streams(spy.loc[ix], sp["pos"].reindex(ix).fillna(0))
    rt, wt = daily_streams(tlt.loc[ix], ts_["pos"].reindex(ix).fillna(0))
    ret = 0.7 * rs + 0.3 * rt
    worst = 0.7 * ws + 0.3 * wt                              # both at their lows at once
    posc = (0.7 * sp["pos"].reindex(ix).fillna(0) + 0.3 * ts_["pos"].reindex(ix).fillna(0))
    posc = posc.shift(1).fillna(0).to_numpy()
    rs_only, ws_only = daily_streams(spy, sp["pos"])
    pos_s = sp["pos"].shift(1).fillna(0).to_numpy()

    firm = {"t1": 0.08, "t2": 0.05, "fee": 399, "split": 0.80}
    print("B · intraday-aware eval (FundingPips) — conservative bound vs close-only")
    print(f"{'candidate':20} {'lev':>4} {'pass%':>6} {'med mo':>7} {'death%':>7} {'EV':>10} {'$/mo':>7}")
    results = []
    for name, (r_, w_, p_) in {
        "FORTRESS 70/30": (ret, worst, posc),
        "SPY portfolio": (rs_only, ws_only, pos_s),
    }.items():
        for lev in (1.0, 1.5, 2.0):
            r = eval_sim_intraday(r_, w_, p_, firm, lev)
            results.append((name, lev, r))
            print(f"{name:20} {lev:4.1f} {r['pass']*100:5.1f}% {r['med_mo']:7.1f} "
                  f"{r['death']*100:6.1f}% ${r['ev']:>+9,.0f} ${r['monthly']:>6,.0f}")
    print("\n(close-only reference: FORTRESS@2x 88.8% pass, $544/mo; "
          "SPY@1.5x 82.2% pass, $425/mo)")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"llm_ict_bot/runs/reports/more_data_{ts}.html"
    a_rows = "".join(f"<tr><td>{n}</td><td>{c1*100:+.1f}%</td><td>{s1:.2f}</td>"
                     f"<td>{c2*100:+.1f}%</td><td>{s2:.2f}</td></tr>"
                     for n, c1, s1, c2, s2 in rows_a)
    b_rows = "".join(f"<tr><td>{n}</td><td>{l:g}x</td><td>{r['pass']*100:.1f}%</td>"
                     f"<td>{r['med_mo']:.1f}</td><td>{r['death']*100:.1f}%</td>"
                     f"<td>${r['ev']:+,.0f}</td><td>${r['monthly']:,.0f}</td></tr>"
                     for n, l, r in results)
    out.write_text(f"""<html><head><title>Test 14 — more data</title>
<style>body{{font-family:system-ui;margin:2em}}table{{border-collapse:collapse;margin:1em 0}}
td,th{{border:1px solid #ccc;padding:4px 10px;font-size:13px;text-align:right}}
td:first-child{{text-align:left}}</style></head><body>
<h1>Test 14 — 64-year validation + intraday-aware eval limits</h1>
<h2>A · ^GSPC 1962-2026: pre-publication era vs modern era</h2>
<table><tr><th>leg</th><th>1962-92 CAGR</th><th>Sharpe</th><th>1993-2026 CAGR</th>
<th>Sharpe</th></tr>{a_rows}</table>
<h2>B · intraday-aware funded sim (worst-mark breaches, conservative bound)</h2>
<table><tr><th>candidate</th><th>lev</th><th>pass</th><th>med months→funded</th>
<th>funded death</th><th>EV 4yr</th><th>$/month</th></tr>{b_rows}</table>
<p><i>Intraday bound assumes the full position rides every instrument's exact low
simultaneously — truth lies between this and the close-only numbers. ^GSPC has no
dividends; strategy relative performance is unaffected.</i></p></body></html>""")
    print(f"\nreport: {out}")

if __name__ == "__main__":
    main()
