"""The rule engine between the model and the broker.

Two responsibilities, kept separate on purpose:

1. ``validate_plan`` — *per-plan* invariants. Pure function. Given a plan,
   the features it was supposed to be based on, and current account size,
   decide whether the plan is allowed and what size it actually gets. This
   is where every LLM-trading failure mode from doc 07 is caught:
   hallucinated setups, inverted R:R, oversize requests.

2. ``RiskState`` — *portfolio/time* halts that span many plans: the daily
   realized-loss cap and the trailing equity floor. These are stateful and
   owned by the engine; a tripped halt blocks new entries until reset (the
   floor requires a manual re-arm — that asymmetry is the point).

The cardinal rule: the model can only ever make things *not* happen here.
It cannot enlarge a cap, cannot bypass a halt, cannot cite a setup that the
deterministic detectors didn't find. If the validator and the model
disagree, the validator wins, silently and always.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from backend.trader.config import RiskConfig
from backend.trader.plan import TradePlan


def catalog_refs(features: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Stable IDs for every detected event, e.g. ``order_blocks:2``.

    The same features always produce the same catalog, so the IDs the model
    is shown in the prompt are exactly the IDs the validator checks against.
    A ref the model invents will not be in this list — and gets the plan
    rejected.
    """
    refs: list[str] = []
    for group, items in features.items():
        for i, _ in enumerate(items):
            refs.append(f"{group}:{i}")
    return refs


@dataclass
class ValidationResult:
    accepted: bool
    reasons: list[str]
    action: str
    side: str | None = None       # "long" | "short" | None
    qty: float = 0.0
    entry: float | None = None
    stop: float | None = None
    take_profit: float | None = None
    risk_cash: float = 0.0
    notional: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return dict(self.__dict__)


def validate_plan(
    plan: TradePlan,
    features: dict[str, list[dict[str, Any]]],
    *,
    equity: float,
    open_symbols: set[str],
    symbol: str,
    risk: RiskConfig,
) -> ValidationResult:
    """Validate and size a single plan. Never raises; rejection is data."""
    # 'wait' is always safe. 'close_existing' is always safe (closing reduces
    # risk); the engine decides whether there's actually something to close.
    if plan.action in ("wait", "close_existing"):
        return ValidationResult(accepted=True, reasons=[], action=plan.action)

    reasons: list[str] = []

    # --- the plan must point at real detector evidence ----------------
    valid_refs = set(catalog_refs(features))
    if not plan.detector_refs:
        reasons.append("no detector_refs: a trade must cite the geometry that justifies it")
    else:
        phantom = [r for r in plan.detector_refs if r not in valid_refs]
        if phantom:
            reasons.append(f"phantom detector_refs not present in features: {phantom}")

    # --- prices must be present and internally consistent -------------
    entry, stop, tp = plan.entry, plan.stop, plan.take_profit
    if entry is None or stop is None or tp is None:
        reasons.append("open_* requires entry, stop and take_profit")
        return ValidationResult(accepted=False, reasons=reasons, action=plan.action)

    if plan.action == "open_long":
        side = "long"
        if not (stop < entry < tp):
            reasons.append(f"long geometry invalid: need stop({stop}) < entry({entry}) < tp({tp})")
        risk_dist = entry - stop
        reward_dist = tp - entry
    else:  # open_short
        side = "short"
        if not (tp < entry < stop):
            reasons.append(f"short geometry invalid: need tp({tp}) < entry({entry}) < stop({stop})")
        risk_dist = stop - entry
        reward_dist = entry - tp

    # --- reward:risk floor (catches the '60% win rate, still lost' trap) ---
    if risk_dist <= 0:
        reasons.append("non-positive risk distance (stop on the wrong side of entry)")
    else:
        rr = reward_dist / risk_dist
        if rr < risk.min_rr:
            reasons.append(f"reward:risk {rr:.2f} below floor {risk.min_rr}")

    # --- concurrency + one-position-per-symbol ------------------------
    if symbol in open_symbols:
        reasons.append(f"already holding a position in {symbol} (no pyramiding in v0)")
    elif len(open_symbols) >= risk.max_open_positions:
        reasons.append(f"position concurrency cap reached ({risk.max_open_positions})")

    if reasons:
        return ValidationResult(accepted=False, reasons=reasons, action=plan.action, side=side)

    # --- sizing: clamp to BOTH the notional cap and the risk cap ------
    # Whichever is smaller wins, so neither cap can ever be exceeded
    # regardless of what size the model asked for.
    requested_pct = plan.size_pct_equity if plan.size_pct_equity is not None else risk.max_position_pct
    requested_pct = max(0.0, min(requested_pct, risk.max_position_pct))
    notional_cap = min(requested_pct, risk.max_symbol_exposure_pct) * equity
    qty_from_notional = notional_cap / entry

    max_risk_cash = risk.max_risk_pct * equity
    qty_from_risk = max_risk_cash / risk_dist

    qty = min(qty_from_notional, qty_from_risk)
    if qty <= 0:
        return ValidationResult(
            accepted=False,
            reasons=["sized to zero (equity too small or caps too tight)"],
            action=plan.action,
            side=side,
        )

    return ValidationResult(
        accepted=True,
        reasons=[],
        action=plan.action,
        side=side,
        qty=qty,
        entry=entry,
        stop=stop,
        take_profit=tp,
        risk_cash=qty * risk_dist,
        notional=qty * entry,
    )


@dataclass
class RiskState:
    """Stateful portfolio halts. Owned by the engine, mutated as P&L lands.

    - ``halted_today``: daily realized-loss cap tripped. Clears at day roll.
    - ``permanent_halt``: trailing equity floor breached. Requires manual
      ``rearm()`` — a bad day shouldn't silently resume tomorrow.
    - ``killed``: the human hit the kill switch. Only ``rearm()`` clears it.
    """

    risk: RiskConfig
    realized_today: float = 0.0
    day_start_equity: float = 0.0
    current_day: date | None = None
    halted_today: bool = False
    permanent_halt: bool = False
    killed: bool = False
    halt_reasons: list[str] = field(default_factory=list)

    def on_equity(self, equity: float, high_water_mark: float, today: date) -> None:
        """Call once per cycle before deciding. Rolls the day and checks the
        trailing equity floor."""
        if self.current_day != today:
            self.current_day = today
            self.realized_today = 0.0
            self.day_start_equity = equity
            self.halted_today = False  # new day clears the soft halt only

        floor = self.risk.equity_floor_pct * high_water_mark
        if equity < floor and not self.permanent_halt:
            self.permanent_halt = True
            self.halt_reasons.append(
                f"equity {equity:.2f} below floor {floor:.2f} "
                f"({self.risk.equity_floor_pct:.0%} of HWM {high_water_mark:.2f}) — manual re-arm required"
            )

    def record_realized(self, net_pnl: float) -> None:
        self.realized_today += net_pnl
        cap = -self.risk.daily_loss_cap_pct * self.day_start_equity
        if self.realized_today <= cap and not self.halted_today:
            self.halted_today = True
            self.halt_reasons.append(
                f"daily realized P&L {self.realized_today:.2f} hit cap {cap:.2f} — halted until tomorrow"
            )

    def entries_allowed(self) -> bool:
        return not (self.killed or self.permanent_halt or self.halted_today)

    def block_reason(self) -> str | None:
        if self.killed:
            return "kill switch engaged"
        if self.permanent_halt:
            return "permanent halt (equity floor) — manual re-arm required"
        if self.halted_today:
            return "daily loss cap — halted until tomorrow"
        return None

    def kill(self) -> None:
        self.killed = True
        self.halt_reasons.append("manual kill switch engaged")

    def rearm(self) -> None:
        self.killed = False
        self.permanent_halt = False
        self.halted_today = False
        self.halt_reasons.append("manually re-armed")

    def status(self) -> dict[str, Any]:
        return {
            "entries_allowed": self.entries_allowed(),
            "killed": self.killed,
            "permanent_halt": self.permanent_halt,
            "halted_today": self.halted_today,
            "realized_today": self.realized_today,
            "block_reason": self.block_reason(),
            "halt_reasons": list(self.halt_reasons),
        }
