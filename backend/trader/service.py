"""Long-running service wrapper: owns the engine, the decision loop, and the
kill switch.

This is the only stateful, long-lived object the trader adds to the
process. It is created once at startup (paper account funded from config)
and driven through a handful of verbs: start, stop, kill, rearm, decide_once.

The loop is deliberately boring: wake, run one cycle per symbol, sleep. A
crash in one symbol's cycle is logged and skipped, never allowed to take the
loop down — a trader that silently dies is worse than one that errors loudly
and keeps its watchdog ticking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.trader.broker import PaperBroker
from backend.trader.config import TraderConfig, load_config
from backend.trader.engine import Engine
from backend.trader.journal import Journal
from backend.trader.risk import RiskState

log = logging.getLogger("hal.trader")


class TraderService:
    def __init__(self, *, notes: str, client: Any = None, config: TraderConfig | None = None) -> None:
        self.config = config or load_config()
        self.broker = PaperBroker(
            starting_equity=self.config.starting_equity,
            slippage_bps=self.config.fills.slippage_bps,
            fee_bps=self.config.fills.fee_bps,
        )
        self.risk_state = RiskState(risk=self.config.risk)
        self.journal = Journal()
        self.engine = Engine(
            config=self.config,
            broker=self.broker,
            risk_state=self.risk_state,
            journal=self.journal,
            notes=notes,
            client=client,
        )
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_tick: float | None = None

    # --- loop control ----------------------------------------------------
    def start(self) -> dict[str, Any]:
        if self.risk_state.killed or self.risk_state.permanent_halt:
            return {"started": False, "reason": self.risk_state.block_reason() + " — call /trader/rearm first"}
        if self._running:
            return {"started": False, "reason": "already running"}
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info("trader loop started: symbols=%s tf=%s", self.config.loop.symbols, self.config.loop.timeframe)
        return {"started": True, "symbols": list(self.config.loop.symbols)}

    async def stop(self) -> dict[str, Any]:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("trader loop stopped")
        return {"stopped": True}

    async def kill(self) -> dict[str, Any]:
        """Hard stop: flatten everything, halt, require manual re-arm."""
        self.risk_state.kill()
        flattened = self.engine.flatten_all("kill_switch")
        await self.stop()
        log.warning("KILL SWITCH: flattened %d position(s)", len(flattened))
        return {"killed": True, "flattened": flattened}

    def rearm(self) -> dict[str, Any]:
        self.risk_state.rearm()
        return {"rearmed": True, "note": "halts cleared; call /trader/start to resume"}

    async def _loop(self) -> None:
        cfg = self.config
        try:
            while self._running:
                self._last_tick = time.time()
                for symbol in cfg.loop.symbols:
                    try:
                        summary = await self.engine.run_cycle(symbol)
                        log.info("cycle %s: %s", symbol, summary)
                    except Exception:
                        log.exception("cycle failed for %s — skipping", symbol)
                await asyncio.sleep(cfg.loop.interval_seconds)
        except asyncio.CancelledError:
            raise

    # --- on-demand + reporting ------------------------------------------
    async def decide_once(self, symbol: str) -> dict[str, Any]:
        """Run a single cycle for one symbol without the loop. For testing
        and manual pokes."""
        return await self.engine.run_cycle(symbol)

    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "last_tick_age_s": (time.time() - self._last_tick) if self._last_tick else None,
            "policy": self.config.policy if self.engine.client is not None else "rule",
            "model": self.engine.model if self.engine.client is not None else None,
            "symbols": list(self.config.loop.symbols),
            "timeframe": self.config.loop.timeframe,
            "account": self.broker.snapshot(self.engine.last_prices),
            "risk": self.risk_state.status(),
        }


# Module-level singleton, initialised by the FastAPI lifespan once notes and
# the genai client exist.
_service: TraderService | None = None


def init_service(notes: str, client: Any) -> TraderService:
    global _service
    _service = TraderService(notes=notes, client=client)
    return _service


def get_service() -> TraderService:
    if _service is None:
        raise RuntimeError("trader service not initialised")
    return _service
