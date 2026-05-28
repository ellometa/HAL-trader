"""Replay the live policy against a window of history — doc 07's Phase C.

The whole point of this module is *not* to invent a second trading code
path. It reuses ``Engine.step`` — the exact function the live loop calls
every cycle — and just feeds it successive prefixes of a historical frame:
``df.iloc[:end]`` for growing ``end``. Each call sees one more bar than the
last, so the bar that was "forming" last cycle is now closed and gets
exit-checked, precisely as it would live. If the backtest and the paper run
ever disagree on the P&L of the same trades, it's a real bug — not a
difference in how the two were computed. That equivalence is the only reason
to trust a backtest number at all.

No look-ahead survives this design for free: ``step`` itself drops the last
(forming) row before detecting, and never exit-checks a position against a
bar that closed at or before its entry. We add nothing that could leak the
future; we only choose where the window ends.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from backend.trader import metrics
from backend.trader.broker import PaperBroker
from backend.trader.config import TraderConfig
from backend.trader.engine import Engine
from backend.trader.journal import Journal
from backend.trader.risk import RiskState


async def run_backtest(
    *,
    symbol: str,
    df: Any,
    config: TraderConfig,
    client: Any = None,
    model: str = "gemini-2.5-flash",
    notes: str = "",
    warmup: int = 50,
    journal_path: Path | None = None,
) -> dict[str, Any]:
    """Run the policy over ``df`` and return a metrics dict.

    ``warmup`` bars are fed to the detectors before the first decision so the
    structure/OB context is populated; with ``client=None`` (or a config whose
    policy is ``"rule"``) this runs the deterministic baseline with no network.
    Leftover open positions are flattened at the final close so ``final_equity``
    is fully realized and the trade list is complete.
    """
    broker = PaperBroker(
        starting_equity=config.starting_equity,
        slippage_bps=config.fills.slippage_bps,
        fee_bps=config.fills.fee_bps,
    )
    risk_state = RiskState(risk=config.risk)

    tmp: Path | None = None
    if journal_path is None:
        fh = tempfile.NamedTemporaryFile(
            prefix="hal-backtest-", suffix=".jsonl", delete=False
        )
        fh.close()
        tmp = Path(fh.name)
        journal_path = tmp

    journal = Journal(journal_path)
    engine = Engine(
        config=config,
        broker=broker,
        risk_state=risk_state,
        journal=journal,
        notes=notes,
        client=client,
        model=model,
    )

    try:
        n = len(df)
        start = max(3, warmup)
        # Each iteration hands ``step`` one more bar. The new last row is the
        # forming bar; the row that was forming last time is now closed and
        # gets its exits resolved — exactly the live sequence.
        for end in range(start, n + 1):
            await engine.step(symbol, df.iloc[:end])

        ending_open = len(broker.positions)
        # Flatten anything still open at the last known price so the result is
        # fully realized. These closes happen outside ``step`` and so are not
        # journaled; the trade list below reads ``broker.closed_trades``, which
        # does include them.
        engine.flatten_all("backtest_end")

        # Confidence is carried on the *open* execution records, not on the
        # ClosedTrade; rebuild the plan_id -> confidence map from the journal
        # so exits can be attributed back to what the model claimed at entry.
        conf_by_plan: dict[str, str] = {}
        for r in journal.read_all():
            ex = r.get("execution") or {}
            if ex.get("executed") and ex.get("plan_id"):
                conf_by_plan[ex["plan_id"]] = ex.get("confidence", "unknown")

        trades = [t.snapshot() for t in broker.closed_trades]
        stats = metrics.compute_stats(trades, conf_by_plan)

        final_equity = broker.equity({})  # flat now, so == realized_equity
        return {
            "symbol": symbol,
            "policy": config.policy if client is not None else "rule",
            "bars": n,
            "decisions": max(0, n - start + 1),
            "warmup": warmup,
            "starting_equity": config.starting_equity,
            "final_equity": final_equity,
            "return_pct": (final_equity / config.starting_equity - 1.0)
            if config.starting_equity
            else None,
            "ending_open_positions": ending_open,
            "high_water_mark": broker.high_water_mark,
            "risk_status": risk_state.status(),
            **stats,
            "trades": trades,
        }
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
