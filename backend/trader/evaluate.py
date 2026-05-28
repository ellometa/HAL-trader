"""Walk-forward evaluation — judge a policy on a distribution, not a window.

A single backtest number is a trap: tune a knob until one BTC slice looks
good and you've fit the noise, not an edge. doc 07 names this as the thing to
avoid. So this module never reports one number. It cuts the history into
contiguous, non-overlapping folds, backtests each fold independently (fresh
equity, its own warmup — no state or look-ahead leaks across folds), and
reports two complementary things:

- the **distribution** across folds: how many were profitable, the median
  and spread of returns. Robustness. An edge that only shows up in one fold
  of five is noise wearing a costume.
- the **pooled** result: every trade from every fold thrown into one bag and
  scored once. The aggregate expectancy. This is the honest "did it make
  money over all of it" number.

A change to the policy is only worth keeping if it moves *both* — more folds
green AND better pooled expectancy. Anything that improves the pooled number
while most folds stay red is almost always overfitting.
"""
from __future__ import annotations

from statistics import median
from typing import Any

from backend.trader import metrics
from backend.trader.backtest import run_backtest
from backend.trader.config import TraderConfig


async def walk_forward(
    *,
    symbol: str,
    df: Any,
    config: TraderConfig,
    client: Any = None,
    model: str = "gemini-2.5-flash",
    notes: str = "",
    warmup: int = 50,
    folds: int = 4,
) -> dict[str, Any]:
    """Split ``df`` into ``folds`` contiguous slices and backtest each.

    Each fold must comfortably exceed ``warmup`` or it can't make a decision;
    folds too small to be meaningful are skipped and noted.
    """
    n = len(df)
    fold_size = n // folds
    fold_reports: list[dict[str, Any]] = []
    all_trades: list[dict[str, Any]] = []
    # plan_ids are unique per (symbol, entry-bar) and folds are disjoint time
    # slices, so merging fold maps never collides — the pooled calibration sees
    # every trade's stated confidence.
    pooled_conf: dict[str, str] = {}

    for k in range(folds):
        lo = k * fold_size
        hi = n if k == folds - 1 else (k + 1) * fold_size
        slice_df = df.iloc[lo:hi]
        if len(slice_df) < warmup + 5:
            fold_reports.append({"fold": k, "skipped": "fold smaller than warmup+5"})
            continue
        r = await run_backtest(
            symbol=symbol,
            df=slice_df,
            config=config,
            client=client,
            model=model,
            notes=notes,
            warmup=warmup,
        )
        all_trades.extend(r["trades"])
        pooled_conf.update(r["conf_by_plan"])
        fold_reports.append({
            "fold": k,
            "bars": r["bars"],
            "closed_trades": r["closed_trades"],
            "return_pct": r["return_pct"],
            "buy_hold_return_pct": r["buy_hold_return_pct"],
            "net_pnl": r["net_pnl"],
            "win_rate": r["win_rate"],
            "profit_factor": r["profit_factor"],
        })

    scored = [f for f in fold_reports if "return_pct" in f and f["return_pct"] is not None]
    returns = [f["return_pct"] for f in scored]
    # Per-fold alpha vs buy-and-hold: the number that actually says "did the
    # policy earn its turnover, or just ride/fight the tape." A fold counts as
    # a win only if it beat simply holding through the same bars.
    bh = [f["buy_hold_return_pct"] for f in scored if f["buy_hold_return_pct"] is not None]
    folds_beat_buyhold = sum(
        1 for f in scored
        if f["buy_hold_return_pct"] is not None and f["return_pct"] > f["buy_hold_return_pct"]
    )
    pooled = metrics.compute_stats(all_trades, pooled_conf)

    return {
        "symbol": symbol,
        "policy": config.policy if client is not None else "rule",
        "bars": n,
        "folds": folds,
        "folds_scored": len(scored),
        "folds_profitable": sum(1 for r in returns if r > 0),
        "folds_beat_buyhold": folds_beat_buyhold,
        "median_return_pct": median(returns) if returns else None,
        "mean_return_pct": (sum(returns) / len(returns)) if returns else None,
        "worst_return_pct": min(returns) if returns else None,
        "best_return_pct": max(returns) if returns else None,
        "median_buyhold_pct": median(bh) if bh else None,
        "pooled_trades": pooled["closed_trades"],
        "pooled_net_pnl": pooled["net_pnl"],
        "pooled_win_rate": pooled["win_rate"],
        "pooled_profit_factor": pooled["profit_factor"],
        # Out-of-sample calibration: does the confidence the policy stamped at
        # entry actually predict wins? If "high" doesn't out-win "low", the
        # confidence signal is noise (or worse, inverted) and can't be trusted.
        "pooled_by_confidence": pooled["by_confidence"],
        "fold_reports": fold_reports,
    }


def _pct(x: float | None) -> str:
    return f"{x * 100:+.2f}%" if x is not None else "—"


def _fnum(x: float | None, fmt: str = ".2f") -> str:
    return format(x, fmt) if x is not None else "—"


def format_walk_forward(report: dict[str, Any]) -> str:
    """Human-readable robustness report for one symbol."""
    L: list[str] = ["=" * 60]
    L.append(f"WALK-FORWARD   {report['symbol']}   policy {report['policy']}")
    L.append(f"  {report['bars']} bars / {report['folds']} folds "
             f"({report['folds_scored']} scored)")
    L.append("-" * 60)
    L.append("per-fold returns:")
    for f in report["fold_reports"]:
        if "skipped" in f:
            L.append(f"  fold {f['fold']}   skipped ({f['skipped']})")
            continue
        beat = "" if f.get("buy_hold_return_pct") is None else (
            " win" if f["return_pct"] > f["buy_hold_return_pct"] else " lose"
        )
        L.append(
            f"  fold {f['fold']}   {_pct(f['return_pct']):>8s}   "
            f"trades {f['closed_trades']:3d}   PF {_fnum(f['profit_factor'])}"
            f"   vs B&H {_pct(f.get('buy_hold_return_pct')):>8s}{beat}"
        )
    L.append("-" * 60)
    L.append(f"folds profitable     {report['folds_profitable']}/{report['folds_scored']}")
    L.append(f"folds beat buy&hold  {report['folds_beat_buyhold']}/{report['folds_scored']}")
    L.append(f"median fold return   {_pct(report['median_return_pct'])}"
             f"   (buy&hold {_pct(report['median_buyhold_pct'])})")
    L.append(f"return spread        {_pct(report['worst_return_pct'])} .. {_pct(report['best_return_pct'])}")
    L.append("-" * 60)
    win = report["pooled_win_rate"]
    win_str = f"{win * 100:.1f}%" if win is not None else "—"
    L.append(
        f"POOLED  trades {report['pooled_trades']}   "
        f"net {report['pooled_net_pnl']:+.2f}   "
        f"win {win_str}   "
        f"PF {_fnum(report['pooled_profit_factor'])}"
    )
    by_conf = report.get("pooled_by_confidence") or {}
    if any(by_conf.get(c, {}).get("n") for c in ("high", "medium", "low")):
        L.append("-" * 60)
        L.append("calibration (does stated confidence predict wins?):")
        for conf in ("high", "medium", "low", "unknown"):
            d = by_conf.get(conf)
            if d and d["n"]:
                wr = d["wins"] / d["n"]
                L.append(f"  {conf:8s} n={d['n']:3d}   net {d['net']:+8.2f}   "
                         f"win {wr * 100:.1f}%")
    L.append("=" * 60)
    return "\n".join(L)
