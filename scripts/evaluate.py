"""Walk-forward evaluation across symbols — the anti-overfitting tool.

Backtests the policy on several symbols, each split into folds, and prints a
robustness report per symbol plus a combined pooled line. Use it to decide
whether a policy change is real: a keeper improves BOTH the share of
profitable folds AND the pooled expectancy. If only the pooled number moves
while most folds stay red, you fit the noise.

The override flags exist precisely so you can A/B a knob without editing the
committed config:

    python -m scripts.evaluate BTCUSDT ETHUSDT --candles 1000 --folds 5
    python -m scripts.evaluate BTCUSDT --candles 1000 --no-management
    python -m scripts.evaluate BTCUSDT --candles 1000 --min-confluence 0.6
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace

from backend.ohlc import fetch_ohlc
from backend.trader.config import TraderConfig, load_config
from backend.trader.evaluate import format_walk_forward, walk_forward


def _apply_overrides(config: TraderConfig, args: argparse.Namespace) -> TraderConfig:
    risk = config.risk
    if args.min_confluence is not None:
        risk = replace(risk, min_confluence=args.min_confluence)
    if args.max_entry_zone_mult is not None:
        risk = replace(risk, max_entry_zone_mult=args.max_entry_zone_mult)
    management = config.management
    if args.no_management:
        management = replace(management, enabled=False)
    return replace(config, risk=risk, management=management)


async def _run(args: argparse.Namespace) -> None:
    config = _apply_overrides(load_config(), args)

    overrides = []
    if args.no_management:
        overrides.append("management OFF")
    if args.min_confluence is not None:
        overrides.append(f"min_confluence={args.min_confluence}")
    if args.max_entry_zone_mult is not None:
        overrides.append(f"max_entry_zone_mult={args.max_entry_zone_mult}")
    if overrides:
        print(f"[overrides: {', '.join(overrides)}]\n")

    combined_profitable = combined_scored = 0
    pooled_net = 0.0
    pooled_trades = 0

    for symbol in args.symbols:
        df = await fetch_ohlc(symbol, args.timeframe, args.candles)
        report = await walk_forward(
            symbol=symbol, df=df, config=config, warmup=args.warmup, folds=args.folds
        )
        print(format_walk_forward(report))
        print()
        combined_profitable += report["folds_profitable"]
        combined_scored += report["folds_scored"]
        pooled_net += report["pooled_net_pnl"]
        pooled_trades += report["pooled_trades"]

    if len(args.symbols) > 1:
        print("#" * 60)
        print(f"ALL SYMBOLS   folds profitable {combined_profitable}/{combined_scored}"
              f"   pooled trades {pooled_trades}   pooled net {pooled_net:+.2f}")
        print("#" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbols", nargs="+", help="e.g. BTCUSDT ETHUSDT")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--candles", type=int, default=1000, help="bars of history to fetch")
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=50)
    # A/B override knobs (do not touch the committed config)
    ap.add_argument("--no-management", action="store_true", help="disable stop management")
    ap.add_argument("--min-confluence", type=float, default=None)
    ap.add_argument("--max-entry-zone-mult", type=float, default=None)
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
