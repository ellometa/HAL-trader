"""Test 11 — FX funded-account simulation: can SLIP-ADX clear a 2-step eval, and
when does it blow?

Simulates buying a $100k FX prop-firm evaluation and trading it with SLIP-ADX —
the only EUR/USD strategy in this project with a positive OOS record (thin: 10
trades). Trade stream bootstrapped from the real Test 8 walk-forward trades
(47 trades, 2021-06 → 2026-06): R-multiples, inter-trade gaps, and holding times
are all resampled from the empirical record.

Firm rule sets (researched 2026-07; sources in RESEARCH_SUMMARY Test 11)
------------------------------------------------------------------------
FTMO 2-step Swing $100k   P1 +10%, P2 +5%, daily −5%, max −10% (static), min 4
                          trading days/phase, no time limit, fee $540, 80% split.
FundingPips 2-step $100k  P1 +8%, P2 +5%, min 3 days, same loss limits, fee $399,
                          80% split.
FundedNext 2-step $100k   P1 +10%, P2 +5%, min 5 days, same loss limits, fee $549,
                          90% split.

Edge scenarios (the honest part — the OOS record is 10 trades)
--------------------------------------------------------------
oos   resample the 10 OOS R-multiples (+0.70R avg)  — optimistic: edge is real
full  resample all 47 R-multiples (+0.17R avg)      — base case
null  47 R-multiples demeaned to 0                   — skeptic: no edge, pure luck

Risk per trade swept over 0.5% / 1% / 2% / 3% of initial balance. One position at
a time (engine invariant) → at ≤3% risk a single trade cannot breach the −5% daily
line, so busts come from the −10% cumulative line. Payouts monthly at the split,
balance resets after payout. Horizon 5 years. 5,000 paths per cell.

Also tracked: P(a gap > 30 calendar days) — several firms flag or terminate
inactive accounts; SLIP-ADX's median gap is 28 days, so this is a real rule risk.

Run:  .venv/bin/python llm_ict_bot/fx_funded_sim.py   (from the HAL repo root)
"""
from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TRADES_CSV = ROOT / "llm_ict_bot/runs/reports/trades_SLIP-ADX_20260711_022934.csv"
RNG = np.random.default_rng(7)
HORIZON_D = 5 * 365
N_SIMS = 5000

FIRMS = {
    "FTMO":        {"t1": 0.10, "t2": 0.05, "fee": 540, "split": 0.80, "min_days": 4},
    "FundingPips": {"t1": 0.08, "t2": 0.05, "fee": 399, "split": 0.80, "min_days": 3},
    "FundedNext":  {"t1": 0.10, "t2": 0.05, "fee": 549, "split": 0.90, "min_days": 5},
}

def load_stream() -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
    df = pd.read_csv(TRADES_CSV, parse_dates=["entry_time", "exit_time"])
    gaps = df["entry_time"].diff().dt.days.dropna().to_numpy().clip(1)
    r_all = df["r_multiple"].to_numpy()
    r_oos = df.loc[df["entry_time"] >= "2025-01-01", "r_multiple"].to_numpy()
    return gaps, r_all, {"oos": r_oos, "full": r_all, "null": r_all - r_all.mean()}

def simulate(r_dist: np.ndarray, gaps: np.ndarray, firm: dict, risk: float,
             n_sims: int = N_SIMS) -> dict:
    """Per path: sequence of trades (gap days apart), each risking `risk` of the
    initial $100k. Phases → funded stage with monthly payouts. Returns summary."""
    max_trades = int(HORIZON_D / max(gaps.min(), 1)) + 8
    R = RNG.choice(r_dist, size=(n_sims, max_trades))
    G = RNG.choice(gaps, size=(n_sims, max_trades))
    days = np.cumsum(G, axis=1)
    pnl = R * risk                                            # fraction of initial

    funded_day = np.full(n_sims, -1.0)
    blow_day = np.full(n_sims, -1.0)
    payout = np.zeros(n_sims)
    gap30 = (G > 30).any(axis=1)
    for s in range(n_sims):
        phase, cum, tgt = 1, 0.0, firm["t1"]
        last_payout_day = 0.0
        for i in range(max_trades):
            d = days[s, i]
            if d > HORIZON_D:
                break
            cum += pnl[s, i]
            if cum <= -0.10:                                  # max-loss line
                if phase == 3:
                    blow_day[s] = d
                else:
                    blow_day[s] = d
                break
            if phase in (1, 2) and cum >= tgt:
                phase += 1
                cum = 0.0
                tgt = firm["t2"]
                if phase == 3:
                    funded_day[s] = d
                    last_payout_day = d
            elif phase == 3 and d - last_payout_day >= 30 and cum > 0:
                payout[s] += cum * 100_000 * firm["split"]    # monthly payout
                cum = 0.0
                last_payout_day = d
    funded = funded_day >= 0
    blown = blow_day >= 0
    ev = payout.mean() - firm["fee"]
    return {"pass": funded.mean(),
            "med_days_fund": np.median(funded_day[funded]) if funded.any() else np.nan,
            "blow": blown.mean(),
            "med_days_blow": np.median(blow_day[blown]) if blown.any() else np.nan,
            "timeout": 1 - funded.mean() - ((blown & ~funded).mean()),
            "mean_payout": payout.mean(), "ev": ev,
            "monthly": payout.mean() / (HORIZON_D / 30.4),
            "gap30": gap30.mean()}

def main():
    gaps, r_all, scen = load_stream()
    print(f"SLIP-ADX stream: {len(r_all)} trades, avg {r_all.mean():+.3f}R, "
          f"median gap {np.median(gaps):.0f}d, P(gap>30d in a path) tracked per sim\n")

    results = {}
    for firm_name, firm in FIRMS.items():
        for scen_name, dist in scen.items():
            for risk in (0.005, 0.01, 0.02, 0.03):
                results[(firm_name, scen_name, risk)] = simulate(dist, gaps, firm, risk)

    for firm_name in FIRMS:
        print(f"=== {firm_name} ($100k, fee ${FIRMS[firm_name]['fee']}, "
              f"split {FIRMS[firm_name]['split']:.0%}) ===")
        print(f"{'edge':>5} {'risk':>5} {'pass%':>6} {'med mo→fund':>12} {'blow%':>6} "
              f"{'med mo→blow':>12} {'timeout%':>9} {'EV':>9} {'$/month':>8}")
        for scen_name in ("oos", "full", "null"):
            for risk in (0.005, 0.01, 0.02, 0.03):
                r = results[(firm_name, scen_name, risk)]
                mf = r["med_days_fund"] / 30.4 if r["med_days_fund"] == r["med_days_fund"] else float("nan")
                mb = r["med_days_blow"] / 30.4 if r["med_days_blow"] == r["med_days_blow"] else float("nan")
                print(f"{scen_name:>5} {risk*100:4.1f}% {r['pass']*100:5.1f}% {mf:12.1f} "
                      f"{r['blow']*100:5.1f}% {mb:12.1f} {r['timeout']*100:8.1f}% "
                      f"${r['ev']:>+8,.0f} ${r['monthly']:>7,.0f}")
        print()
    g30 = results[("FTMO", "full", 0.01)]["gap30"]
    print(f"inactivity-rule risk: {g30*100:.0f}% of paths hit a >30-day trade gap "
          f"(firms may flag/terminate; FTMO allows requesting a freeze)\n")

    # --- plot: pass/blow by risk (FTMO, all scenarios) + EV comparison ---------------
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    risks = [0.5, 1, 2, 3]
    for scen_name, ls in (("oos", "-o"), ("full", "-s"), ("null", "-x")):
        axes[0].plot(risks, [results[("FTMO", scen_name, r / 100)]["pass"] * 100
                             for r in risks], ls, label=scen_name)
        axes[1].plot(risks, [results[("FTMO", scen_name, r / 100)]["blow"] * 100
                             for r in risks], ls, label=scen_name)
        axes[2].plot(risks, [results[("FTMO", scen_name, r / 100)]["ev"]
                             for r in risks], ls, label=scen_name)
    axes[0].set_title("FTMO pass rate %"); axes[1].set_title("blow rate % (5yr)")
    axes[2].set_title("EV $ net of fee (5yr)"); axes[2].axhline(0, color="red", ls="--")
    for ax in axes:
        ax.set_xlabel("risk per trade %"); ax.grid(alpha=0.3); ax.legend()
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110)
    img = base64.b64encode(buf.getvalue()).decode()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"llm_ict_bot/runs/reports/fx_funded_{ts}.html"
    trs = ""
    for (fn, sn, rk), r in results.items():
        mf = f"{r['med_days_fund']/30.4:.1f}" if r["med_days_fund"] == r["med_days_fund"] else "—"
        mb = f"{r['med_days_blow']/30.4:.1f}" if r["med_days_blow"] == r["med_days_blow"] else "—"
        trs += (f"<tr><td>{fn}</td><td>{sn}</td><td>{rk*100:.1f}%</td>"
                f"<td>{r['pass']*100:.1f}%</td><td>{mf}</td><td>{r['blow']*100:.1f}%</td>"
                f"<td>{mb}</td><td>{r['timeout']*100:.1f}%</td>"
                f"<td>${r['ev']:+,.0f}</td><td>${r['monthly']:,.0f}</td></tr>")
    out.write_text(f"""<html><head><title>Test 11 — FX funded-account sim (SLIP-ADX)</title>
<style>body{{font-family:system-ui;margin:2em}}table{{border-collapse:collapse;margin:1em 0}}
td,th{{border:1px solid #ccc;padding:4px 10px;font-size:13px;text-align:right}}
td:first-child,td:nth-child(2){{text-align:left}}</style></head><body>
<h1>Test 11 — FX funded-account simulation: SLIP-ADX vs 2-step evals</h1>
<p>Trade stream bootstrapped from the real 47-trade SLIP-ADX walk-forward record
(median 28d between trades). Edge scenarios: <b>oos</b> = the 10 OOS trades (+0.70R,
optimistic), <b>full</b> = all 47 (+0.17R, base), <b>null</b> = demeaned (no edge).
5,000 paths × 5 years per cell. One position at a time → the −5% daily line is
unreachable at ≤3% risk; busts come from the −10% max-loss line.</p>
<table><tr><th>firm</th><th>edge</th><th>risk/trade</th><th>pass</th>
<th>med months→funded</th><th>blow</th><th>med months→blow</th><th>timeout</th>
<th>EV (5yr, net fee)</th><th>$/month</th></tr>{trs}</table>
<img src="data:image/png;base64,{img}" style="max-width:100%">
<p><i>Honesty: the oos scenario rests on 10 trades; the full-record avg (+0.17R) is
the defensible base. Inactivity: a large share of paths include >30-day trade gaps —
a soft rule risk not modeled as termination. Fees: FTMO $540 / FundingPips $399 /
FundedNext $549; splits 80/80/90%.</i></p></body></html>""")
    print(f"report: {out}")

if __name__ == "__main__":
    main()
