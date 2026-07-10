"""Test 10 — stress tests + FTMO-style funded-account Monte Carlo.

Takes the Test 9 survivors (SPY strategy portfolio) and asks the two questions that
matter before money: (1) is the edge a parameter spike or a plateau? (2) can it pass —
and keep — a prop-firm funded account, and what is the honest expected value?

Stress battery
--------------
S1 Parameter perturbation  — every leg's parameters wiggled ±~30%; a real edge is a
   plateau (neighbors profitable), a fake one is a spike.
S2 Block bootstrap         — stationary 5-day block resampling of portfolio daily
   returns, 2,000 paths → CAGR / max-DD distributions instead of one lucky path.
S3 Decade sub-periods      — 1993-2002 / 2003-2012 / 2013-2019 / 2020-2026.
S4 Cost sensitivity        — 1 / 2 / 5 / 10 bps per side.

Funded-account simulation (FTMO Swing 2-step, $100k, per ftmo.com 2026)
-----------------------------------------------------------------------
Phase 1 +10%, Phase 2 +5%; max daily loss 5% and max total loss 10%, both static on
initial balance; no time limit; Swing = overnight/weekend holding allowed; fee ≈ $540
(refunded at first payout); 80% profit split; monthly payouts reset balance to $100k.
Strategy = SPY PORTFOLIO avg at leverage L (US500 CFD), financing 6%/yr on the
leveraged notional above cash, evaluated at L ∈ {1,2,3,4,5} over 3,000 bootstrap
paths of 1,000 trading days (~4 years).

Honesty notes: daily-loss breaches are checked close-to-close (intraday spikes are
invisible on daily bars → bust risk is *understated*); bootstrap assumes the 33-year
return distribution persists; CFD spread on US500 ≈ our 1 bp assumption.

Run:  .venv/bin/python llm_ict_bot/stress_funded.py   (from the HAL repo root)
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
from pivot_screen import (COST, ROOT, double7, ibs_strategy, load_equity, metrics,
                          rsi, run, spy_end_of_month, spy_portfolio,
                          spy_turnaround_tuesday)

RNG = np.random.default_rng(42)
PPY = 252

def ann(net: pd.Series) -> tuple[float, float, float]:
    eq = (1 + net).cumprod()
    yrs = len(net) / PPY
    cagr = eq.iloc[-1] ** (1 / yrs) - 1
    shp = net.mean() / net.std() * np.sqrt(PPY) if net.std() > 0 else np.nan
    dd = (eq / eq.cummax() - 1).min()
    return cagr, shp, dd

# --- S1 · parameter perturbation --------------------------------------------------------
def perturb(spy: pd.DataFrame) -> list[tuple[str, float, float]]:
    out = []
    for lo, hi in [(8, 75), (8, 85), (10, 75), (10, 85), (12, 80), (5, 80), (15, 80)]:
        r2, ma200, ma5 = rsi(spy["close"], 2), spy["close"].rolling(200).mean(), spy["close"].rolling(5).mean()
        pos = pd.Series(np.nan, index=spy.index)
        pos[(r2 < lo) & (spy["close"] > ma200)] = 1.0
        pos[spy["close"] > ma5] = 0.0
        c, s, d = ann(run(pos.ffill().fillna(0.0), spy, COST["SPY"], "close")["net"])
        out.append((f"RSI2 entry<{lo} (200MA)", c, s))
    for n in (5, 6, 7, 8, 9):
        c_, ma200 = spy["close"], spy["close"].rolling(200).mean()
        lo_, hi_ = c_.rolling(n).min(), c_.rolling(n).max()
        pos = pd.Series(np.nan, index=spy.index)
        pos[(c_ > ma200) & (c_ <= lo_)] = 1.0
        pos[c_ >= hi_] = 0.0
        c, s, d = ann(run(pos.ffill().fillna(0.0), spy, COST["SPY"], "close")["net"])
        out.append((f"Double-{n}s", c, s))
    for et, xt in [(0.15, 0.8), (0.2, 0.7), (0.2, 0.9), (0.25, 0.8), (0.3, 0.75)]:
        rng_ = spy["high"] - spy["low"]
        ibs = ((spy["close"] - spy["low"]) / rng_.where(rng_ > 0)).fillna(0.5)
        pos = pd.Series(np.nan, index=spy.index)
        pos[ibs < et] = 1.0
        pos[ibs > xt] = 0.0
        c, s, d = ann(run(pos.ffill().fillna(0.0), spy, COST["SPY"], "close")["net"])
        out.append((f"IBS <{et}/>{xt}", c, s))
    return out

# --- S2 · block bootstrap ---------------------------------------------------------------
def block_bootstrap(net: np.ndarray, n_days: int, n_sims: int, block: int = 5) -> np.ndarray:
    """Stationary block bootstrap → (n_sims, n_days) daily-return paths."""
    n = len(net)
    n_blocks = n_days // block + 1
    starts = RNG.integers(0, n - block, size=(n_sims, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_sims, -1)
    return net[idx[:, :n_days]]

# --- funded-account state machine (vectorized over sims) --------------------------------
def ftmo_sim(net: np.ndarray, pos: np.ndarray, lev: float, n_sims: int = 3000,
             horizon: int = 1000, fee: float = 540.0) -> dict:
    """net/pos: historical daily net strategy returns and exposure (aligned). Simulates
    FTMO Swing $100k at leverage `lev` with 6%/yr financing on exposure above cash."""
    fin = 0.06 / PPY * np.maximum(0.0, lev * pos - 1.0)
    lr = lev * net - fin                                     # levered daily returns
    paths = block_bootstrap(lr, horizon, n_sims)

    P1, P2, FUNDED, DEAD = 0, 1, 2, 3
    state = np.zeros(n_sims, dtype=int)
    bal = np.full(n_sims, 100_000.0)                         # balance within phase
    payout = np.zeros(n_sims)
    funded_day = np.full(n_sims, -1)
    days_in_month = 0
    for d in range(horizon):
        r = paths[:, d]
        live = state != DEAD
        pnl = bal * r * live
        bal = bal + pnl
        # static limits: daily loss ≥ $5k, total equity < $90k
        dead_now = live & ((pnl <= -5_000) | (bal < 90_000))
        state[dead_now] = DEAD
        # phase transitions
        p1_pass = (state == P1) & (bal >= 110_000) & ~dead_now
        state[p1_pass] = P2; bal[p1_pass] = 100_000.0
        p2_pass = (state == P2) & (bal >= 105_000) & ~dead_now
        state[p2_pass] = FUNDED; bal[p2_pass] = 100_000.0
        funded_day[p2_pass & (funded_day < 0)] = d
        # monthly payout in funded stage: 80% of profit, balance resets
        days_in_month += 1
        if days_in_month == 21:
            days_in_month = 0
            f = (state == FUNDED) & (bal > 100_000)
            payout[f] += (bal[f] - 100_000) * 0.8
            bal[f] = 100_000.0
    got_funded = funded_day >= 0
    died_after_funding = got_funded & (state == DEAD)
    ev = payout.mean() - fee
    return {"lev": lev,
            "pass_rate": got_funded.mean(),
            "med_days_to_fund": (np.median(funded_day[got_funded]) if got_funded.any() else np.nan),
            "bust_challenge": ((state == DEAD) & ~got_funded).mean(),
            "funded_death": (died_after_funding.mean() / got_funded.mean() if got_funded.any() else np.nan),
            "mean_payout": payout.mean(),
            "p50_payout": np.median(payout),
            "ev": ev,
            "monthly_income": payout.mean() / (horizon / 21)}

# --- main -------------------------------------------------------------------------------
def main():
    spy = load_equity("SPY")
    port = spy_portfolio(spy, "avg")
    net, pos = port["net"].to_numpy(), port["pos"].to_numpy()
    c0, s0, d0 = ann(port["net"])
    print(f"SPY PORTFOLIO avg — full period: CAGR {c0*100:.1f}%, Sharpe {s0:.2f}, MaxDD {d0*100:.0f}%\n")

    print("S1 · parameter perturbation (plateau test) — every neighbor should stay positive")
    pert = perturb(spy)
    for name, c, s in pert:
        print(f"  {name:24} CAGR {c*100:5.1f}%  Sharpe {s:5.2f}")
    worst = min(p[2] for p in pert)
    print(f"  → worst neighbor Sharpe: {worst:.2f}\n")

    print("S2 · block bootstrap of the portfolio (2,000 × 33yr-sampled 2yr paths)")
    bs = block_bootstrap(net, 504, 2000)
    eq = (1 + bs).cumprod(axis=1)
    cagr2 = eq[:, -1] ** (PPY / 504) - 1
    dd2 = (eq / np.maximum.accumulate(eq, axis=1) - 1).min(axis=1)
    print(f"  2yr CAGR percentiles  5/50/95: {np.percentile(cagr2, 5)*100:+.1f}% / "
          f"{np.percentile(cagr2, 50)*100:+.1f}% / {np.percentile(cagr2, 95)*100:+.1f}%")
    print(f"  2yr MaxDD percentiles 5/50/95: {np.percentile(dd2, 5)*100:.1f}% / "
          f"{np.percentile(dd2, 50)*100:.1f}% / {np.percentile(dd2, 95)*100:.1f}%")
    print(f"  P(2yr loss): {(cagr2 < 0).mean()*100:.1f}%\n")

    print("S3 · decade sub-periods (portfolio)")
    eras = [("1993-2002", "1993", "2003"), ("2003-2012", "2003", "2013"),
            ("2013-2019", "2013", "2020"), ("2020-2026", "2020", "2027")]
    for name, a, b in eras:
        sl = port["net"][(port["net"].index >= a) & (port["net"].index < b)]
        if len(sl) > PPY:
            c, s, d = ann(sl)
            print(f"  {name}: CAGR {c*100:5.1f}%  Sharpe {s:5.2f}  MaxDD {d*100:5.1f}%")
    print()

    print("S4 · cost sensitivity (per side)")
    for bps in (1, 2, 5, 10):
        r = run(port["pos"], spy, bps / 10_000, "close")
        c, s, d = ann(r["net"])
        print(f"  {bps:>2} bps: CAGR {c*100:5.1f}%  Sharpe {s:5.2f}")
    print()

    print("FTMO Swing $100k Monte Carlo — SPY PORTFOLIO avg, 3,000 paths × 4yr")
    print(f"{'lev':>4} {'pass%':>6} {'med days→fund':>14} {'challenge bust%':>16} "
          f"{'funded death%':>14} {'mean payout':>12} {'EV vs $540':>11} {'$/month':>9}")
    frows = []
    for lev in (1, 2, 3, 4, 5):
        r = ftmo_sim(net, pos, lev)
        frows.append(r)
        print(f"{lev:>4} {r['pass_rate']*100:5.1f}% {r['med_days_to_fund']:>14.0f} "
              f"{r['bust_challenge']*100:>15.1f}% {r['funded_death']*100:>13.1f}% "
              f"${r['mean_payout']:>10,.0f} ${r['ev']:>+9,.0f} ${r['monthly_income']:>8,.0f}")

    # --- report ---------------------------------------------------------------------
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    axes[0].hist(cagr2 * 100, bins=60); axes[0].axvline(0, color="red", ls="--")
    axes[0].set_title("S2 · bootstrap 2yr CAGR (%)")
    axes[1].hist(dd2 * 100, bins=60); axes[1].axvline(-10, color="red", ls="--")
    axes[1].set_title("S2 · bootstrap 2yr MaxDD (%) — red = FTMO kill line")
    axes[2].plot([r["lev"] for r in frows], [r["ev"] for r in frows], "o-")
    axes[2].axhline(0, color="red", ls="--"); axes[2].set_xlabel("leverage")
    axes[2].set_title("FTMO EV vs $540 fee, by leverage ($, 4yr horizon)")
    for ax in axes: ax.grid(alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110)
    img = base64.b64encode(buf.getvalue()).decode()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"llm_ict_bot/runs/reports/stress_funded_{ts}.html"
    pert_rows = "".join(f"<tr><td>{n}</td><td>{c*100:+.1f}%</td><td>{s:.2f}</td></tr>"
                        for n, c, s in pert)
    frow_html = "".join(
        f"<tr><td>{r['lev']}x</td><td>{r['pass_rate']*100:.1f}%</td>"
        f"<td>{r['med_days_to_fund']:.0f}</td><td>{r['bust_challenge']*100:.1f}%</td>"
        f"<td>{r['funded_death']*100:.1f}%</td><td>${r['mean_payout']:,.0f}</td>"
        f"<td>${r['ev']:+,.0f}</td><td>${r['monthly_income']:,.0f}</td></tr>" for r in frows)
    out.write_text(f"""<html><head><title>Test 10 — stress + funded-account sim</title>
<style>body{{font-family:system-ui;margin:2em}}table{{border-collapse:collapse;margin:1em 0}}
td,th{{border:1px solid #ccc;padding:4px 10px;font-size:13px;text-align:right}}
td:first-child{{text-align:left}}</style></head><body>
<h1>Test 10 — stress tests + FTMO funded-account Monte Carlo</h1>
<p>Strategy: <b>SPY PORTFOLIO avg</b> (RSI-2+200MA, Turnaround Tuesday, End-of-month,
IBS, Double 7s — equal weight). Full period: CAGR {c0*100:.1f}%, Sharpe {s0:.2f},
MaxDD {d0*100:.0f}%.</p>
<h2>S1 · parameter plateau</h2><table><tr><th>neighbor</th><th>CAGR</th><th>Sharpe</th></tr>
{pert_rows}</table>
<h2>FTMO Swing $100k (fee $540, 80% split, monthly payouts, 4yr horizon)</h2>
<table><tr><th>leverage</th><th>pass rate</th><th>median days→funded</th>
<th>challenge bust</th><th>funded acct death</th><th>mean total payout</th>
<th>EV net of fee</th><th>mean $/month</th></tr>{frow_html}</table>
<img src="data:image/png;base64,{img}" style="max-width:100%">
<p><i>Honesty: daily-loss checked close-to-close only (intraday breaches invisible →
bust risk understated); bootstrap assumes history's return distribution persists;
financing 6%/yr on levered notional; $540 fee refunds at first payout (not modeled —
EV slightly understated in compensation).</i></p></body></html>""")
    print(f"\nreport: {out}")

if __name__ == "__main__":
    main()
