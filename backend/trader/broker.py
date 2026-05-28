"""Paper broker — the only broker HAL has.

Accounting model: a margin/CFD-style account, not spot. We track
``realized_equity`` (settled balance) and compute *total* equity as
realized + floating P&L of open positions. Opening a position does not
move cash (only the entry fee is charged); closing settles the realized
P&L minus the exit fee. This sidesteps notional-cash bookkeeping and
matches how the derivatives venues these strategies target actually feel.

Fill realism (the part that kills naive backtests):
- Market entries get **adverse slippage**: longs fill higher, shorts lower.
- Stop-losses get adverse slippage too — stops gap through in real life.
- Take-profits are limit orders: they fill at the target, no positive
  slippage gift, but no adverse slippage either.
- Every fill, both sides, pays a taker fee.

No look-ahead lives here: ``check_exits`` is fed the high/low of a *closed*
bar by the engine. When a single bar touches both stop and target we assume
the stop filled first. Pessimism is the correct bias for a thing that
trades by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Side = Literal["long", "short"]
ExitReason = Literal["take_profit", "stop_loss", "time_stop", "manual", "kill_switch"]


def _iso(t: Any) -> Any:
    return t.isoformat() if hasattr(t, "isoformat") else t


@dataclass
class Position:
    symbol: str
    side: Side
    qty: float
    entry_price: float          # post-slippage fill price
    stop: float
    take_profit: float
    entry_time: Any
    validity_bars: int
    plan_id: str
    entry_fee: float

    def unrealized(self, mark: float) -> float:
        if self.side == "long":
            return (mark - self.entry_price) * self.qty
        return (self.entry_price - mark) * self.qty

    def notional(self, price: float) -> float:
        return abs(self.qty) * price

    def snapshot(self, mark: float) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "entry_price": self.entry_price,
            "stop": self.stop,
            "take_profit": self.take_profit,
            "entry_time": _iso(self.entry_time),
            "validity_bars": self.validity_bars,
            "plan_id": self.plan_id,
            "mark": mark,
            "unrealized": self.unrealized(mark),
        }


@dataclass
class ClosedTrade:
    symbol: str
    side: Side
    qty: float
    entry_price: float
    exit_price: float
    entry_time: Any
    exit_time: Any
    gross_pnl: float
    fees: float
    net_pnl: float
    reason: ExitReason
    plan_id: str

    def snapshot(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["entry_time"] = _iso(self.entry_time)
        d["exit_time"] = _iso(self.exit_time)
        return d


@dataclass
class PaperBroker:
    starting_equity: float
    slippage_bps: float
    fee_bps: float
    realized_equity: float = field(init=False)
    high_water_mark: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    closed_trades: list[ClosedTrade] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.realized_equity = self.starting_equity
        self.high_water_mark = self.starting_equity

    # --- pricing helpers -------------------------------------------------
    def _slip(self, price: float, *, adverse_for: Side, exiting: bool) -> float:
        """Adverse slippage. For an entry we fill worse for our side; for an
        exit the directionality flips (closing a long is a sell)."""
        frac = self.slippage_bps / 1e4
        buying = (adverse_for == "long") != exiting  # long-entry or short-exit => buying
        return price * (1 + frac) if buying else price * (1 - frac)

    def _fee(self, notional: float) -> float:
        return notional * self.fee_bps / 1e4

    # --- equity ----------------------------------------------------------
    def equity(self, marks: dict[str, float]) -> float:
        floating = sum(
            p.unrealized(marks.get(sym, p.entry_price))
            for sym, p in self.positions.items()
        )
        eq = self.realized_equity + floating
        self.high_water_mark = max(self.high_water_mark, eq)
        return eq

    # --- lifecycle -------------------------------------------------------
    def open_position(
        self,
        *,
        symbol: str,
        side: Side,
        qty: float,
        requested_price: float,
        stop: float,
        take_profit: float,
        entry_time: Any,
        validity_bars: int,
        plan_id: str,
    ) -> Position:
        fill = self._slip(requested_price, adverse_for=side, exiting=False)
        fee = self._fee(abs(qty) * fill)
        self.realized_equity -= fee
        pos = Position(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=fill,
            stop=stop,
            take_profit=take_profit,
            entry_time=entry_time,
            validity_bars=validity_bars,
            plan_id=plan_id,
            entry_fee=fee,
        )
        self.positions[symbol] = pos
        return pos

    def _settle(
        self, pos: Position, exit_price: float, reason: ExitReason, exit_time: Any
    ) -> ClosedTrade:
        gross = pos.unrealized(exit_price)
        exit_fee = self._fee(pos.notional(exit_price))
        net = gross - exit_fee
        self.realized_equity += net
        trade = ClosedTrade(
            symbol=pos.symbol,
            side=pos.side,
            qty=pos.qty,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            entry_time=pos.entry_time,
            exit_time=exit_time,
            gross_pnl=gross,
            fees=pos.entry_fee + exit_fee,
            net_pnl=net,
            reason=reason,
            plan_id=pos.plan_id,
        )
        self.closed_trades.append(trade)
        del self.positions[pos.symbol]
        return trade

    def close_at_market(
        self, symbol: str, requested_price: float, reason: ExitReason, exit_time: Any
    ) -> ClosedTrade | None:
        pos = self.positions.get(symbol)
        if pos is None:
            return None
        fill = self._slip(requested_price, adverse_for=pos.side, exiting=True)
        return self._settle(pos, fill, reason, exit_time)

    def check_exits(
        self, symbol: str, bar_high: float, bar_low: float, exit_time: Any
    ) -> ClosedTrade | None:
        """Resolve stop/target against a *closed* bar's high/low.

        Stop checked before target (pessimistic when a bar straddles both).
        Stops get adverse slippage; targets (limit orders) fill exactly.
        """
        pos = self.positions.get(symbol)
        if pos is None:
            return None

        if pos.side == "long":
            if bar_low <= pos.stop:
                fill = pos.stop * (1 - self.slippage_bps / 1e4)
                return self._settle(pos, fill, "stop_loss", exit_time)
            if bar_high >= pos.take_profit:
                return self._settle(pos, pos.take_profit, "take_profit", exit_time)
        else:  # short
            if bar_high >= pos.stop:
                fill = pos.stop * (1 + self.slippage_bps / 1e4)
                return self._settle(pos, fill, "stop_loss", exit_time)
            if bar_low <= pos.take_profit:
                return self._settle(pos, pos.take_profit, "take_profit", exit_time)
        return None

    # --- reporting -------------------------------------------------------
    def snapshot(self, marks: dict[str, float]) -> dict[str, Any]:
        eq = self.equity(marks)
        return {
            "realized_equity": self.realized_equity,
            "equity": eq,
            "high_water_mark": self.high_water_mark,
            "open_positions": [
                p.snapshot(marks.get(s, p.entry_price))
                for s, p in self.positions.items()
            ],
            "closed_trades": len(self.closed_trades),
        }
