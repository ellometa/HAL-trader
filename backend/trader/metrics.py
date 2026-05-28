"""Shared performance math for closed trades.

One implementation, used by both the live journal review
(``scripts/replay_journal.py``) and the backtest harness, so "net P&L after
costs" means exactly the same thing whether the trades came from a 30-day
paper run or a historical replay. Divergence between those two numbers is
the bug Phase C exists to catch; sharing the math removes one way for them
to lie to each other.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


def _pct(x: float | None) -> str:
    return f"{100 * x:.1f}%" if x is not None else "—"


def compute_stats(trades: list[dict[str, Any]], conf_by_plan: dict[str, str]) -> dict[str, Any]:
    """Aggregate a list of closed-trade snapshots into the metrics that
    actually matter. ``conf_by_plan`` maps plan_id -> stated confidence so we
    can ask whether 'high' confidence really outperforms 'low'."""
    n = len(trades)
    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in losses)

    by_reason: dict[str, dict[str, Any]] = {}
    for reason in Counter(t["reason"] for t in trades):
        rt = [t for t in trades if t["reason"] == reason]
        by_reason[reason] = {"count": len(rt), "net": sum(t["net_pnl"] for t in rt)}

    by_conf: dict[str, dict[str, Any]] = {}
    cmap: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        cmap[conf_by_plan.get(t.get("plan_id", ""), "unknown")].append(t["net_pnl"])
    for conf, pnls in cmap.items():
        by_conf[conf] = {"n": len(pnls), "net": sum(pnls), "wins": sum(1 for x in pnls if x > 0)}

    sides = Counter(t["side"] for t in trades)
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n) if n else None,
        "net_pnl": sum(t["net_pnl"] for t in trades),
        "fees": sum(t.get("fees", 0.0) for t in trades),
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "avg_win": (gross_win / len(wins)) if wins else None,
        "avg_loss": (-gross_loss / len(losses)) if losses else None,
        "by_reason": by_reason,
        "by_confidence": by_conf,
        "long": sides.get("long", 0),
        "short": sides.get("short", 0),
    }


def format_stats(stats: dict[str, Any], *, title: str = "", header_lines: list[str] | None = None) -> str:
    L: list[str] = ["=" * 60]
    if title:
        L.append(title)
    for h in header_lines or []:
        L.append(h)
    if header_lines:
        L.append("-" * 60)

    n = stats["closed_trades"]
    L.append(f"closed trades        {n}")
    L.append(f"  wins / losses      {stats['wins']} / {stats['losses']}   win rate {_pct(stats['win_rate'])}")
    L.append(f"  net P&L (after fees){stats['net_pnl']:+.2f}")
    L.append(f"  total fees paid     {stats['fees']:.2f}")
    if stats["profit_factor"] is not None:
        L.append(f"  profit factor       {stats['profit_factor']:.2f}")
    if stats["avg_win"] is not None:
        L.append(f"  avg win             {stats['avg_win']:+.2f}")
    if stats["avg_loss"] is not None:
        L.append(f"  avg loss            {stats['avg_loss']:+.2f}")

    L.append("-" * 60)
    L.append("exit reasons:")
    for reason, d in sorted(stats["by_reason"].items(), key=lambda kv: -kv[1]["count"]):
        L.append(f"  {reason:12s} {d['count']:4d}   net {d['net']:+.2f}")

    L.append("-" * 60)
    L.append("P&L by stated confidence at entry:")
    for conf in ("high", "medium", "low", "unknown"):
        d = stats["by_confidence"].get(conf)
        if d:
            wr = (d["wins"] / d["n"]) if d["n"] else None
            L.append(f"  {conf:8s} n={d['n']:3d}  net {d['net']:+.2f}  win {_pct(wr)}")

    if n:
        total = stats["long"] + stats["short"]
        L.append("-" * 60)
        L.append(f"direction mix        long {stats['long']} / short {stats['short']}"
                 f"   ({_pct(stats['long'] / total if total else None)} long)")
    L.append("=" * 60)
    return "\n".join(L)
