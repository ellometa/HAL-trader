"""One decision cycle, start to finish.

    fetch OHLC → drop the forming bar → run detectors on CLOSED bars only
    → resolve exits on the last closed bar → check halts → get a plan
    → validate + size → (maybe) fill → journal everything.

No-look-ahead is enforced in exactly two places and it matters in both:
1. Detection runs on ``df.iloc[:-1]`` — the last row may be a forming candle,
   and a detector that sees the future is worthless.
2. A freshly opened position is never exit-checked against a bar that closed
   at or before its entry. We only resolve stops/targets against bars whose
   timestamp is strictly after the entry time.

The engine holds no policy of its own. It fetches, sequences, and records;
every yes/no comes from ``risk`` and every fill from the ``PaperBroker``.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from backend.ict.detector import detect_all
from backend.ohlc import fetch_ohlc
from backend.trader import brain
from backend.trader.broker import PaperBroker
from backend.trader.config import TraderConfig
from backend.trader.journal import Journal
from backend.trader.plan import TradePlan
from backend.trader.risk import RiskState, validate_plan

log = logging.getLogger("hal.trader")


class Engine:
    def __init__(
        self,
        *,
        config: TraderConfig,
        broker: PaperBroker,
        risk_state: RiskState,
        journal: Journal,
        notes: str,
        client: Any = None,
        model: str = "gemini-2.5-flash",
    ) -> None:
        self.config = config
        self.broker = broker
        self.risk_state = risk_state
        self.journal = journal
        self.notes = notes
        self.client = client
        self.model = model
        # Best-available marks across symbols, so equity (and the floor check)
        # reflects floating P&L on positions we didn't price this cycle.
        self.last_prices: dict[str, float] = {}

    async def run_cycle(self, symbol: str) -> dict[str, Any]:
        """Live cycle: fetch the latest candles, then decide. The fetch is the
        only thing that separates live from backtest — the decision logic in
        ``step`` is shared, so a backtest exercises the exact same policy."""
        cfg = self.config
        df = await fetch_ohlc(symbol, cfg.loop.timeframe, cfg.loop.candles)
        return await self.step(symbol, df)

    async def step(self, symbol: str, df: Any) -> dict[str, Any]:
        """One decision over a window of candles. ``df``'s last row is treated
        as the forming bar (its close is 'now'); detection runs on the closed
        bars before it. Backtest feeds successive windows; live feeds the live
        fetch. Same code path either way."""
        cfg = self.config
        if len(df) < 3:
            return {"symbol": symbol, "skipped": "not enough candles"}

        closed = df.iloc[:-1]                       # drop the forming bar
        current_price = float(df["close"].iloc[-1])
        live_time = df["timestamp"].iloc[-1]
        last_closed = closed.iloc[-1]
        last_closed_time = closed["timestamp"].iloc[-1]
        self.last_prices[symbol] = current_price

        features = detect_all(closed)

        # --- 1. resolve exits on the last CLOSED bar -------------------
        exits: list[dict[str, Any]] = []
        pos = self.broker.positions.get(symbol)
        if pos is not None and last_closed_time > pos.entry_time:
            closed_trade = self.broker.check_exits(
                symbol, float(last_closed["high"]), float(last_closed["low"]), last_closed_time
            )
            if closed_trade is None:
                bars_held = int((closed["timestamp"] > pos.entry_time).sum())
                if bars_held >= pos.validity_bars:
                    closed_trade = self.broker.close_at_market(
                        symbol, current_price, "time_stop", last_closed_time
                    )
            if closed_trade is not None:
                self.risk_state.record_realized(closed_trade.net_pnl)
                exits.append(closed_trade.snapshot())

        # --- 1b. manage the stop on the last CLOSED bar ----------------
        # Ratchet the stop toward profit (breakeven, then trailing). Uses the
        # same no-look-ahead gate as exits: never on the entry bar, and only
        # closed-bar prices. The move takes effect for *subsequent* bars, so a
        # favourable excursion observed now protects the trade next cycle.
        stop_moves: list[dict[str, Any]] = []
        mpos = self.broker.positions.get(symbol)
        if mpos is not None and cfg.management.enabled and last_closed_time > mpos.entry_time:
            moved = self.broker.manage_stops(
                symbol,
                float(last_closed["high"]),
                float(last_closed["low"]),
                breakeven_at_r=cfg.management.breakeven_at_r,
                trail_at_r=cfg.management.trail_at_r,
                trail_r=cfg.management.trail_r,
            )
            if moved is not None:
                stop_moves.append(moved)

        # --- 2. equity + halt bookkeeping ------------------------------
        equity = self.broker.equity(self.last_prices)
        today = (live_time.to_pydatetime() if hasattr(live_time, "to_pydatetime") else live_time)
        today = today.astimezone(timezone.utc).date() if today.tzinfo else today.date()
        self.risk_state.on_equity(equity, self.broker.high_water_mark, today)

        # --- 3. get a plan ---------------------------------------------
        pos = self.broker.positions.get(symbol)
        position_side = pos.side if pos else None
        portfolio = {
            "equity": equity,
            "open_position": pos.snapshot(current_price) if pos else None,
            "open_symbols": list(self.broker.positions.keys()),
            "risk": self.risk_state.status(),
        }

        if cfg.policy == "rule" or self.client is None:
            plan = brain.rule_plan(
                features=features,
                current_price=current_price,
                risk=cfg.risk,
                position_side=position_side,
                validity_bars=cfg.loop.default_validity_bars,
                n_bars=len(closed),
            )
            system_prompt = user_message = "(deterministic rule policy — no model call)"
        else:
            plan, system_prompt, user_message = await brain.llm_plan(
                client=self.client,
                model=self.model,
                symbol=symbol,
                timeframe=cfg.loop.timeframe,
                current_price=current_price,
                features=features,
                portfolio=portfolio,
                notes=self.notes,
                risk=cfg.risk,
            )

        # --- 4. act on the plan ----------------------------------------
        execution = await self._execute(symbol, plan, features, equity, current_price, live_time)

        # --- 5. journal everything -------------------------------------
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "timeframe": cfg.loop.timeframe,
            "policy": cfg.policy if self.client is not None else "rule",
            "current_price": current_price,
            "equity": equity,
            "high_water_mark": self.broker.high_water_mark,
            "features": features,
            "plan": plan.model_dump(),
            "execution": execution,
            "exits": exits,
            "stop_moves": stop_moves,
            "risk_status": self.risk_state.status(),
            "prompt": {"system": system_prompt, "user": user_message},
        }
        self.journal.append(record)
        return {
            "symbol": symbol,
            "action": plan.action,
            "executed": execution.get("executed", False),
            "reasons": execution.get("validation", {}).get("reasons", []),
            "exits": exits,
            "equity": equity,
        }

    async def _execute(
        self,
        symbol: str,
        plan: TradePlan,
        features: dict[str, Any],
        equity: float,
        current_price: float,
        live_time: Any,
    ) -> dict[str, Any]:
        # Discretionary close requested by the policy.
        if plan.action == "close_existing":
            trade = self.broker.close_at_market(symbol, current_price, "manual", live_time)
            if trade is not None:
                self.risk_state.record_realized(trade.net_pnl)
                return {"executed": True, "closed": trade.snapshot()}
            return {"executed": False, "note": "no position to close"}

        if plan.action == "wait":
            return {"executed": False, "note": "wait"}

        # open_long / open_short — gate on halts first, then validate.
        if not self.risk_state.entries_allowed():
            return {
                "executed": False,
                "blocked": self.risk_state.block_reason(),
                "validation": {"accepted": False, "reasons": [self.risk_state.block_reason()]},
            }

        result = validate_plan(
            plan,
            features,
            equity=equity,
            open_symbols=set(self.broker.positions.keys()),
            symbol=symbol,
            risk=self.config.risk,
        )
        if not result.accepted:
            return {"executed": False, "validation": result.snapshot()}

        # Market fill at the live price; stop/target come from the validated
        # plan. (v0 simplification: we do not honour the model's exact entry
        # level — a market order fills at market. Documented in engine docstring.)
        plan_id = f"{symbol}-{int(time.time())}"
        self.broker.open_position(
            symbol=symbol,
            side=result.side,  # type: ignore[arg-type]
            qty=result.qty,
            requested_price=current_price,
            stop=result.stop,  # type: ignore[arg-type]
            take_profit=result.take_profit,  # type: ignore[arg-type]
            entry_time=live_time,
            validity_bars=plan.validity_bars,
            plan_id=plan_id,
        )
        # plan_id + confidence recorded so the journal can later join a closed
        # trade's P&L back to the confidence the model stated at entry.
        return {
            "executed": True,
            "plan_id": plan_id,
            "confidence": plan.confidence,
            "validation": result.snapshot(),
        }

    def flatten_all(self, reason: str = "kill_switch") -> list[dict[str, Any]]:
        """Close every open position at last known price. Used by the kill
        switch and on shutdown."""
        out: list[dict[str, Any]] = []
        for symbol in list(self.broker.positions.keys()):
            price = self.last_prices.get(symbol, self.broker.positions[symbol].entry_price)
            trade = self.broker.close_at_market(symbol, price, reason, datetime.now(timezone.utc))  # type: ignore[arg-type]
            if trade is not None:
                self.risk_state.record_realized(trade.net_pnl)
                out.append(trade.snapshot())
        return out
