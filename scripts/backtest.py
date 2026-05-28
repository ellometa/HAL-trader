"""Backtest the live trading policy against recent history.

This is doc 07's Phase C in CLI form. It fetches candles, replays them
through the *exact* engine the paper loop uses (``Engine.step``), and prints
the same report ``replay_journal`` prints for live runs — net P&L after
costs, by confidence, by direction — because both go through
``trader.metrics``. A backtest number and a paper number are therefore
comparable by construction.

Usage:
    python -m scripts.backtest BTCUSDT
    python -m scripts.backtest ETHUSDT --timeframe 15m --candles 500 --warmup 80
    python -m scripts.backtest BTCUSDT --policy llm     # needs GEMINI_API_KEY

By default it runs the deterministic rule policy (no network, no API key).
``--policy llm`` calls Gemini for every decision — slow and quota-hungry over
hundreds of bars, so reach for it deliberately.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from backend.ohlc import fetch_ohlc
from backend.trader import metrics
from backend.trader.backtest import run_backtest
from backend.trader.config import load_config

_NOTES_PATH = Path(__file__).resolve().parent.parent / "backend" / "notes.md"


async def _run(args: argparse.Namespace) -> None:
    config = load_config()

    client = None
    notes = ""
    if args.policy == "llm":
        # Import lazily: the rule policy must not require a Gemini key.
        from google import genai

        from backend.config import GEMINI_API_KEY

        client = genai.Client(api_key=GEMINI_API_KEY)
        if _NOTES_PATH.exists():
            notes = _NOTES_PATH.read_text(encoding="utf-8")

    df = await fetch_ohlc(args.symbol, args.timeframe, args.candles)
    result = await run_backtest(
        symbol=args.symbol,
        df=df,
        config=config,
        client=client,
        notes=notes,
        warmup=args.warmup,
    )

    header = [
        f"symbol               {result['symbol']}   policy {result['policy']}",
        f"window               {result['bars']} {args.timeframe} bars"
        f"   ({result['decisions']} decisions, {result['warmup']} warmup)",
        f"starting equity      {result['starting_equity']:.2f}",
        f"final equity         {result['final_equity']:.2f}"
        f"   ({result['return_pct'] * 100:+.2f}%)"
        if result["return_pct"] is not None
        else f"final equity         {result['final_equity']:.2f}",
    ]
    if result["ending_open_positions"]:
        header.append(
            f"(flattened {result['ending_open_positions']} open position(s) at last close)"
        )
    print(metrics.format_stats(result, title="BACKTEST", header_lines=header))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("symbol", help="e.g. BTCUSDT")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--candles", type=int, default=300, help="how many bars to fetch")
    ap.add_argument("--warmup", type=int, default=50, help="bars before the first decision")
    ap.add_argument("--policy", choices=("rule", "llm"), default="rule")
    asyncio.run(_run(ap.parse_args()))


if __name__ == "__main__":
    main()
