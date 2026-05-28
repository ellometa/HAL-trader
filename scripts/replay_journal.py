"""Read the decision journal and print a performance + behaviour report.

This is the weekly-review tool doc 07 asks for: net P&L after costs (the
only metric that matters), win rate, profit factor, and the breakdowns that
catch the specific LLM failure modes — P&L conditional on stated confidence
(is "high" actually better than "low"?) and the long/short ratio (is the
model stuck with a direction bias?).

The performance math itself lives in ``backend.trader.metrics`` and is shared
with the backtest harness, so "net P&L after costs" means the same thing for
a 30-day paper run as it does for a historical replay. This file only does
the journal-specific part: turning cycle records into the (trades,
confidence-map) inputs that math expects, plus the cycle-level context
(how many decisions, which symbols, what was proposed).

Usage:
    python -m scripts.replay_journal [--path data/journal.jsonl]
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from backend.trader import metrics
from backend.trader.journal import Journal


def report(records: list[dict]) -> str:
    cycles = len(records)
    by_symbol = Counter(r["symbol"] for r in records)
    actions = Counter(r["plan"]["action"] for r in records)

    # Map plan_id -> stated confidence at entry, so exits can be attributed.
    conf_by_plan: dict[str, str] = {}
    for r in records:
        ex = r.get("execution") or {}
        if ex.get("executed") and ex.get("plan_id"):
            conf_by_plan[ex["plan_id"]] = ex.get("confidence", "unknown")

    # Every closed trade lives in some cycle's exits array.
    trades = [t for r in records for t in (r.get("exits") or [])]

    stats = metrics.compute_stats(trades, conf_by_plan)
    header = [
        f"cycles logged        {cycles}",
        f"  by symbol          {dict(by_symbol)}",
        f"  actions proposed   {dict(actions)}",
    ]
    return metrics.format_stats(stats, header_lines=header)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", type=Path, default=None, help="path to journal.jsonl")
    args = ap.parse_args()
    records = Journal(args.path).read_all()
    if not records:
        print("journal is empty — nothing to replay")
        return
    print(report(records))


if __name__ == "__main__":
    main()
