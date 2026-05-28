"""Typed, immutable view of ``config/trader.toml``.

Parsed once with stdlib ``tomllib`` (Python 3.11+) into frozen dataclasses
so nothing downstream can mutate a risk cap at runtime. If the file is
missing or a key is absent, we fail loudly at load time rather than
silently trading with a default that nobody chose.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "trader.toml"


@dataclass(frozen=True)
class RiskConfig:
    max_position_pct: float
    max_risk_pct: float
    min_rr: float
    max_open_positions: int
    max_symbol_exposure_pct: float
    daily_loss_cap_pct: float
    equity_floor_pct: float
    # Confluence quality gate: the minimum deterministic confluence score
    # (0..1) a setup must clear before any entry is allowed. Filters the
    # marginal, fee-churning trades. Defaulted so older configs still load.
    min_confluence: float = 0.5
    # No-chase guard: max distance (in zone-heights) price may sit from the
    # zone edge and still be a valid entry. Keeps stops tight and targets
    # reachable. Defaulted so older configs still load.
    max_entry_zone_mult: float = 1.0


@dataclass(frozen=True)
class FillConfig:
    slippage_bps: float
    fee_bps: float


@dataclass(frozen=True)
class LoopConfig:
    symbols: tuple[str, ...]
    timeframe: str
    interval_seconds: int
    candles: int
    default_validity_bars: int


@dataclass(frozen=True)
class TraderConfig:
    policy: str
    starting_equity: float
    quote_currency: str
    risk: RiskConfig
    fills: FillConfig
    loop: LoopConfig


def load_config(path: Path | None = None) -> TraderConfig:
    p = path or _CONFIG_PATH
    if not p.exists():
        raise FileNotFoundError(
            f"Trader config not found at {p}. Copy config/trader.toml into place."
        )
    with p.open("rb") as fh:
        raw = tomllib.load(fh)

    risk = RiskConfig(**raw["risk"])
    fills = FillConfig(**raw["fills"])
    loop_raw = dict(raw["loop"])
    loop = LoopConfig(
        symbols=tuple(loop_raw.pop("symbols")),
        **loop_raw,
    )
    return TraderConfig(
        policy=raw["mode"]["policy"],
        starting_equity=raw["account"]["starting_equity"],
        quote_currency=raw["account"]["quote_currency"],
        risk=risk,
        fills=fills,
        loop=loop,
    )
