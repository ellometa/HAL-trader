"""Test 12 — the eval lab: strategies engineered explicitly to pass funded evals.

The eval objective is NOT Sharpe. It is: hit +8-10% before −10%, quickly, then keep
the account alive. That rewards (a) return per unit of max-drawdown, (b) speed,
(c) suppressed tail days (the −5% daily line). Candidates below are built from the
Test 9/10 replicated legs — no new folklore, only re-engineering of verified edges:

  SPY portfolio      Test 10 champion (5 legs, equal weight) — the baseline.
  QQQ portfolio      same published rules on QQQ/US100 (IBS, RSI-2, Double 7s, TT) —
                     ~1.5× the vol of SPY → faster target-hitting per unit time.
  MULTI 50/50        SPY portfolio + QQQ portfolio, half each — diversification buys
                     drawdown room, which buys leverage inside the eval's risk budget.
  MULTI vol-target   MULTI with a 10%-ann volatility-targeting overlay (20d realized,
                     scale 0.25–2×) — the evidence-backed way to cut the tail days
                     that hit daily-loss lines. [standard vol-targeting literature]
  SPY TT alone       fastest single leg, as a speed benchmark.

Excluded on evidence: gold/XAUUSD mean reversion — published backtest is *negative*
(23% win, PF 0.60 daily, quant-signals 8,693-trade study). Martingale/grid/HFT-style
"challenge passing EAs" — banned by every firm and blow by construction.

Each candidate runs through the funded-account state machine (same mechanics as
Test 10) under two firms — FTMO Swing $100k (+10%/+5%, $540, 80%) and FundingPips
$100k (+8%/+5%, $399, 80%) — at leverage 1–4×, 3,000 paths × 4 years. Instruments
map to US500/US100 CFDs (dividend-adjusted proxies; financing 6%/yr above cash).

Run:  .venv/bin/python llm_ict_bot/eval_lab.py   (from the HAL repo root)
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
                          rsi2_plain, run, spy_portfolio, spy_turnaround_tuesday)
from stress_funded import PPY, ann, block_bootstrap

RNG = np.random.default_rng(11)
FIRMS = {
    "FundingPips": {"t1": 0.08, "t2": 0.05, "fee": 399, "split": 0.80},
    "FTMO":        {"t1": 0.10, "t2": 0.05, "fee": 540, "split": 0.80},
}

def tt_on(px: pd.DataFrame) -> dict:                      # TT rules, any instrument
    return spy_turnaround_tuesday(px)

def qqq_portfolio(qqq: pd.DataFrame) -> dict:
    legs = pd.DataFrame({
        "ibs": ibs_strategy(qqq, "QQQ")["pos"],
        "rsi2": rsi2_plain(qqq, "QQQ")["pos"],
        "d7": double7(qqq, "QQQ")["pos"],
        "tt": tt_on(qqq)["pos"],
    })
    return run(legs.mean(axis=1), qqq, COST["QQQ"], fill="close")

def combine(a: dict, b: dict) -> tuple[pd.Series, pd.Series]:
    ix = a["net"].index.intersection(b["net"].index)
    net = 0.5 * a["net"].reindex(ix).fillna(0) + 0.5 * b["net"].reindex(ix).fillna(0)
    pos = 0.5 * a["pos"].reindex(ix).fillna(0) + 0.5 * b["pos"].reindex(ix).fillna(0)
    return net, pos

def vol_target(net: pd.Series, pos: pd.Series, tv: float = 0.10) -> tuple[pd.Series, pd.Series]:
    rv = net.rolling(20).std() * np.sqrt(PPY)
    lever = (tv / rv).clip(0.25, 2.0).shift(1).fillna(1.0)   # knowable at t-1
    return net * lever, pos * lever

def eval_sim(net: np.ndarray, pos: np.ndarray, firm: dict, lev: float,
             n_sims: int = 3000, horizon: int = 1000) -> dict:
    fin = 0.06 / PPY * np.maximum(0.0, lev * pos - 1.0)
    paths = block_bootstrap(lev * net - fin, horizon, n_sims)
    P1, P2, FUNDED, DEAD = 0, 1, 2, 3
    state = np.zeros(n_sims, dtype=int)
    bal = np.full(n_sims, 100_000.0)
    payout = np.zeros(n_sims)
    funded_day = np.full(n_sims, -1)
    dim = 0
    for d in range(horizon):
        r = paths[:, d]
        live = state != DEAD
        pnl = bal * r * live
        bal = bal + pnl
        dead = live & ((pnl <= -5_000) | (bal < 90_000))
        state[dead] = DEAD
        p1 = (state == P1) & (bal >= 100_000 * (1 + firm["t1"])) & ~dead
        state[p1] = P2; bal[p1] = 100_000.0
        p2 = (state == P2) & (bal >= 100_000 * (1 + firm["t2"])) & ~dead
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
    spy, qqq = load_equity("SPY"), load_equity("QQQ")
    sp = spy_portfolio(spy, "avg")
    qp = qqq_portfolio(qqq)
    m_net, m_pos = combine(sp, qp)
    v_net, v_pos = vol_target(m_net, m_pos)
    tt = spy_turnaround_tuesday(spy)
    cands = {
        "SPY portfolio":    (sp["net"], sp["pos"]),
        "QQQ portfolio":    (qp["net"], qp["pos"]),
        "MULTI 50/50":      (m_net, m_pos),
        "MULTI vol-target": (v_net, v_pos),
        "SPY TT alone":     (tt["net"], tt["pos"]),
    }
    print("candidate profiles (full history):")
    for name, (net, pos) in cands.items():
        c, s, d = ann(net.fillna(0))
        print(f"  {name:18} CAGR {c*100:5.1f}%  Sharpe {s:4.2f}  MaxDD {d*100:5.1f}%  "
              f"return/DD {c/abs(d):4.2f}")
    print()

    results = {}
    for fname, firm in FIRMS.items():
        print(f"=== {fname} (+{firm['t1']*100:.0f}%/+{firm['t2']*100:.0f}%, "
              f"fee ${firm['fee']}) — best leverage per candidate ===")
        print(f"{'candidate':18} {'lev':>4} {'pass%':>6} {'med mo→fund':>12} "
              f"{'funded death%':>14} {'EV 4yr':>9} {'$/month':>8}")
        for name, (net, pos) in cands.items():
            n, p = net.fillna(0).to_numpy(), pos.fillna(0).to_numpy()
            best = None
            for lev in (1.0, 1.5, 2.0, 3.0, 4.0):
                r = eval_sim(n, p, firm, lev)
                r["lev"] = lev
                results[(fname, name, lev)] = r
                if best is None or r["ev"] > best["ev"]:
                    best = r
            print(f"{name:18} {best['lev']:4.1f} {best['pass']*100:5.1f}% "
                  f"{best['med_mo']:12.1f} {best['death']*100:13.1f}% "
                  f"${best['ev']:>+8,.0f} ${best['monthly']:>7,.0f}")
        print()

    # --- report ---------------------------------------------------------------------
    fig, axes = plt.subplots(1, 2, figsize=(14, 4.8))
    levs = [1.0, 1.5, 2.0, 3.0, 4.0]
    for name in cands:
        axes[0].plot(levs, [results[("FundingPips", name, l)]["ev"] for l in levs],
                     "-o", label=name, ms=4)
        axes[1].plot(levs, [results[("FundingPips", name, l)]["pass"] * 100 for l in levs],
                     "-o", label=name, ms=4)
    axes[0].set_title("FundingPips EV $ (4yr, net fee) by leverage")
    axes[0].axhline(0, color="red", ls="--")
    axes[1].set_title("pass rate % by leverage")
    for ax in axes:
        ax.set_xlabel("leverage"); ax.grid(alpha=0.3); ax.legend(fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110)
    img = base64.b64encode(buf.getvalue()).decode()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"llm_ict_bot/runs/reports/eval_lab_{ts}.html"
    trs = ""
    for (fn, name, lev), r in results.items():
        trs += (f"<tr><td>{fn}</td><td>{name}</td><td>{lev:g}x</td>"
                f"<td>{r['pass']*100:.1f}%</td><td>{r['med_mo']:.1f}</td>"
                f"<td>{r['death']*100:.1f}%</td><td>${r['ev']:+,.0f}</td>"
                f"<td>${r['monthly']:,.0f}</td></tr>")
    out.write_text(f"""<html><head><title>Test 12 — eval lab</title>
<style>body{{font-family:system-ui;margin:2em}}table{{border-collapse:collapse;margin:1em 0}}
td,th{{border:1px solid #ccc;padding:4px 10px;font-size:13px;text-align:right}}
td:first-child,td:nth-child(2){{text-align:left}}</style></head><body>
<h1>Test 12 — strategies engineered explicitly to pass funded evals</h1>
<p>All candidates are re-engineered from Test 9's replicated legs (no new folklore).
Instruments map to US500/US100 CFDs. 3,000 paths × 4 years per cell; financing 6%/yr
above cash; monthly payouts at the split; −5% daily / −10% total static lines.</p>
<table><tr><th>firm</th><th>candidate</th><th>lev</th><th>pass</th>
<th>med months→funded</th><th>funded death</th><th>EV (4yr)</th><th>$/month</th></tr>
{trs}</table>
<img src="data:image/png;base64,{img}" style="max-width:100%">
<p><i>Excluded on published evidence: XAUUSD mean reversion (negative expectancy,
8,693-trade study); martingale/grid/HFT "pass services" (banned + blow by design).
Daily-loss checked close-to-close → real bust risk slightly higher.</i></p>
</body></html>""")
    print(f"report: {out}")

if __name__ == "__main__":
    main()
