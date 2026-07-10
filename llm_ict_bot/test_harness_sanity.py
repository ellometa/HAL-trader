"""Robustness audit for the Test 9-12 harnesses (pivot_screen / stress_funded /
eval_lab). Five executable invariants — run before trusting any headline number.

T1 Lookahead (truncation invariance): delete the future → positions before the cut
   must not change. THE definitive leakage test. (End-of-month is exempted: it uses
   the month's trading-day count, which is exchange-calendar knowledge, not price
   lookahead — documented simplification.)
T2 Accounting identity: constant full position at zero cost must reproduce
   close[-1]/close[0] exactly.
T3 Cost sanity: higher costs must never increase final equity, and the cost drag for
   a known trade count must match hand arithmetic.
T4 Bootstrap fidelity: resampled mean/std must converge to the sample's.
T5 Eval-sim limiting behavior: an always-up return stream must pass ~100% and never
   blow; an always-down stream must blow 100% and never pass.

Run:  .venv/bin/python llm_ict_bot/test_harness_sanity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pivot_screen import (COST, double7, ibs_strategy, load_equity, rsi2_plain,
                          run, spy_portfolio, spy_rsi2_200ma,
                          spy_turnaround_tuesday)
from stress_funded import block_bootstrap
from eval_lab import eval_sim

FAIL = 0

def check(name: str, ok: bool, detail: str = "") -> None:
    global FAIL
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    FAIL += 0 if ok else 1

def main():
    spy = load_equity("SPY")

    print("T1 · lookahead / truncation invariance (positions must not depend on the future)")
    cut = len(spy) - 500
    spy_trunc = spy.iloc[:cut]
    strategies = {
        "rsi2_plain": lambda px: rsi2_plain(px, "SPY"),
        "rsi2_200ma": spy_rsi2_200ma,
        "turnaround_tuesday": spy_turnaround_tuesday,
        "ibs": lambda px: ibs_strategy(px, "SPY"),
        "double7": lambda px: double7(px, "SPY"),
    }
    for name, fn in strategies.items():
        full = fn(spy)["pos"].iloc[: cut - 1]
        part = fn(spy_trunc)["pos"].iloc[: cut - 1]
        same = (full.to_numpy() == part.to_numpy()).all()
        check(f"{name}: pos invariant under future deletion", bool(same))

    print("T2 · accounting identity (pos=1, cost=0 ⇒ equity = close[-1]/close[0])")
    r = run(pd.Series(1.0, index=spy.index), spy, 0.0, "close")
    want = spy["close"].iloc[-1] / spy["close"].iloc[0]
    got = r["eq"].iloc[-1]
    check("buy-hold reproduction", abs(got / want - 1) < 1e-9, f"got {got:.4f} want {want:.4f}")

    print("T3 · cost monotonicity + hand arithmetic")
    port = spy_portfolio(spy, "avg")
    eqs = [run(port["pos"], spy, b / 1e4, "close")["eq"].iloc[-1] for b in (0, 1, 5, 10, 20)]
    check("equity strictly decreasing in cost", all(a > b for a, b in zip(eqs, eqs[1:])))
    # one round trip of a full position at 10 bps/side ≈ 20 bps drag
    idx = spy.index[:10]
    px = spy.iloc[:10]
    pos = pd.Series([0, 1, 1, 0, 0, 0, 0, 0, 0, 0], index=idx, dtype=float)
    net0 = run(pos, px, 0.0, "close")["net"].sum()
    net1 = run(pos, px, 10 / 1e4, "close")["net"].sum()
    check("round-trip drag ≈ 2 × cost", abs((net0 - net1) - 2 * 10 / 1e4) < 1e-12)

    print("T4 · bootstrap fidelity")
    net = port["net"].to_numpy()
    bs = block_bootstrap(net, 504, 4000)
    check("mean within 5%", abs(bs.mean() / net.mean() - 1) < 0.05,
          f"sample {net.mean():.2e} vs bs {bs.mean():.2e}")
    check("std within 10%", abs(bs.std() / net.std() - 1) < 0.10)

    print("T5 · eval-sim limiting behavior")
    up = np.full(3000, 0.002); po = np.ones(3000)
    firm = {"t1": 0.08, "t2": 0.05, "fee": 399, "split": 0.8}
    r_up = eval_sim(up, po, firm, 1.0, n_sims=500, horizon=300)
    check("always-up passes ~100%", r_up["pass"] > 0.99, f"pass {r_up['pass']:.3f}")
    dn = np.full(3000, -0.002)
    r_dn = eval_sim(dn, po, firm, 1.0, n_sims=500, horizon=300)
    check("always-down never passes", r_dn["pass"] == 0.0)
    check("always-down EV = -fee", abs(r_dn["ev"] + firm["fee"]) < 1e-9)

    print(f"\n{'ALL CHECKS PASSED' if FAIL == 0 else f'{FAIL} CHECK(S) FAILED'}")
    sys.exit(1 if FAIL else 0)

if __name__ == "__main__":
    main()
