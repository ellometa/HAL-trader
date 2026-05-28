"""Read the decision journal and print a performance + behaviour report.

This is the weekly-review tool doc 07 asks for: net P&L after costs (the
only metric that matters), win rate, profit factor, and the breakdowns that
catch the specific LLM failure modes — P&L conditional on stated confidence
(is "high" actually better than "low"?) and the rolling long/short ratio
(is the model stuck with a direction bias?).

Usage:
    python -m scripts.replay_journal [--path data/journal.jsonl]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from backend.trader.journal import Journal


def _pct(n: int, d: int) -> str:
    return f"{(100 * n / d):.1f}%" if d else "—"


def report(records: list[dict]) -> str:
    lines: list[str] = []
    cycles = len(records)
    by_symbol = Counter(r["symbol"] for r in records)
    actions = Counter(r["plan"]["action"] for r in records)

    # Map plan_id -> stated confidence at entry, so exits can be attributed.
    conf_by_plan: dict[str, str] = {}
    for r in records:
        ex = r.get("execution") or {}
        if ex.get("executed") and ex.get("plan_id"):
            conf_by_plan[ex["plan_id"]] = ex.get("confidence", "unknown")

    # Collect every closed trade from the exits arrays.
    trades = [t for r in records for t in (r.get("exits") or [])]
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)
    net = sum(t["net_pnl"] for t in trades)
    fees = sum(t.get("fees", 0.0) for t in trades)

    lines.append("=" * 60)
    lines.append(f"cycles logged        {cycles}")
    lines.append(f"  by symbol          {dict(by_symbol)}")
    lines.append(f"  actions proposed   {dict(actions)}")
    lines.append("-" * 60)
    lines.append(f"closed trades        {len(trades)}")
    lines.append(f"  wins / losses      {len(wins)} / {len(losses)}   win rate {_pct(len(wins), len(trades))}")
    lines.append(f"  net P&L (after fees){net:+.2f}")
    lines.append(f"  total fees paid     {fees:.2f}")
    if gross_loss > 0:
        lines.append(f"  profit factor       {gross_win / gross_loss:.2f}")
    if wins:
        lines.append(f"  avg win             {gross_win / len(wins):+.2f}")
    if losses:
        lines.append(f"  avg loss            {-gross_loss / len(losses):+.2f}")

    # Exit-reason mix — too many time-stops or stops says the entries are off.
    lines.append("-" * 60)
    lines.append("exit reasons:")
    for reason, c in Counter(t["reason"] for t in trades).most_common():
        rnet = sum(t["net_pnl"] for t in trades if t["reason"] == reason)
        lines.append(f"  {reason:12s} {c:4d}   net {rnet:+.2f}")

    # Confidence calibration — does 'high' actually outperform 'low'?
    lines.append("-" * 60)
    lines.append("P&L by stated confidence at entry:")
    by_conf: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_conf[conf_by_plan.get(t.get("plan_id", ""), "unknown")].append(t["net_pnl"])
    for conf in ("high", "medium", "low", "unknown"):
        pnls = by_conf.get(conf)
        if pnls:
            w = sum(1 for x in pnls if x > 0)
            lines.append(f"  {conf:8s} n={len(pnls):3d}  net {sum(pnls):+.2f}  win {_pct(w, len(pnls))}")

    # Direction bias — the Haiku-86%-long failure mode.
    side_counter = Counter(t["side"] for t in trades)
    if trades:
        longs = side_counter.get("long", 0)
        lines.append("-" * 60)
        lines.append(f"direction mix        long {longs} / short {side_counter.get('short', 0)}"
                     f"   ({_pct(longs, len(trades))} long)")

    lines.append("=" * 60)
    return "\n".join(lines)


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
