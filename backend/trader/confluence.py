"""Confluence scoring — the trader's actual edge, made of detector facts.

The honest meta-note from doc 07 is that the model isn't the edge; the
deterministic geometry layer is. This module is that layer's sharp end. It
takes the raw detector output and asks a single, ICT-shaped question: *how
many independent, aligned, still-valid signals stack up behind a trade right
here?* A lone order block is a guess. An unmitigated order block, in the
direction of the last structure break, backed by an unfilled fair-value gap,
right after price swept the opposing liquidity — that's an A+ setup, and the
difference is measurable.

Everything here is a pure function of the features dict and the current
price. No model, no randomness, no look-ahead: it only reads events the
detectors already emitted from closed bars. The model cannot inflate a
score, and (once wired into the validator) cannot take a trade this module
rates as garbage. That asymmetry is the whole point — the model can only
ever make a trade *less* likely to happen, never more.

Scoring is deliberately legible: every point added carries a human-readable
reason string, so a journal entry can answer "why was this a 0.8?" from the
factors alone. Weights live in one block at the top so they're auditable and
tunable without hunting through logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- weights (audit + tune here, not in the logic below) -----------------
# Points are additive; the normalized score is points / _MAX_POINTS.
W_ZONE = 2          # a real, unmitigated order block to enter against
W_STRUCTURE = 2     # last structure break agrees with the trade direction
W_CHOCH = 1         # ...and it was a change-of-character (reversal) — bonus
W_FVG = 2           # an unfilled fair-value gap backs the move
W_SWEEP = 2         # opposing liquidity was swept just before (stop-run)
W_FRESH = 1         # the zone formed recently (not stale context)
_MAX_POINTS = W_ZONE + W_STRUCTURE + W_CHOCH + W_FVG + W_SWEEP + W_FRESH  # 10

DEFAULT_RECENT_WINDOW = 20  # bars; what counts as "fresh" / "just swept"

# No-chase guard: an entry is only valid if price is within this many zone
# heights of the zone's near edge. The whole ICT idea is to enter *on a
# return to the zone*, not to market-buy after price has already run away —
# chasing makes the structural stop far, the reward:risk target unreachable,
# and the trade times out. ``None`` disables the guard.
DEFAULT_MAX_ENTRY_ZONE_MULT = 1.0


@dataclass
class Setup:
    """A scored, ready-to-size trade candidate. ``entry``/``stop`` come from
    real geometry (current price + the zone edge); the caller adds the target
    from its own reward:risk policy. ``detector_refs`` are catalog IDs in the
    exact format the validator checks, so a plan built from a Setup cannot
    cite a phantom."""

    side: str                       # "long" | "short"
    entry: float
    stop: float
    score: float                    # 0..1, == points / _MAX_POINTS
    points: int
    factors: list[str] = field(default_factory=list)
    detector_refs: list[str] = field(default_factory=list)
    anchor_ref: str = ""            # the order-block ref the setup is built on

    def snapshot(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _structure_bias(features: dict[str, list[dict[str, Any]]]) -> tuple[str | None, bool, str | None]:
    """Return (bias, is_choch, ref) for the most recent structure event."""
    structure = features.get("structure", [])
    if not structure:
        return None, False, None
    last = structure[-1]
    t = last["type"]
    bias = "bullish" if t.endswith("bullish") else "bearish" if t.endswith("bearish") else None
    return bias, t.startswith("choch"), f"structure:{len(structure) - 1}"


def _near_zone(price: float, low: float, high: float, side: str, mult: float | None) -> bool:
    """No-chase guard: is ``price`` close enough to the zone to enter?

    For a long, price must not be more than ``mult`` zone-heights above the
    zone's high; for a short, not more than ``mult`` below the zone's low.
    ``mult=None`` disables the guard (any distance allowed)."""
    if mult is None:
        return True
    height = max(high - low, 1e-9)
    if side == "long":
        return price <= high + mult * height
    return price >= low - mult * height


def _score_long(
    features: dict[str, list[dict[str, Any]]],
    current_price: float,
    *,
    n_bars: int,
    recent_window: int,
    max_entry_zone_mult: float | None,
) -> Setup | None:
    obs = features.get("order_blocks", [])
    # Valid long zones: unmitigated bullish OBs sitting below price (support
    # we can buy a retrace into), and close enough that we're not chasing.
    # Tightest stop (highest low) ranks first as a tie-break, since closer
    # support is a better reward:risk.
    candidates = [
        (i, ob)
        for i, ob in enumerate(obs)
        if ob["type"] == "ob_bullish"
        and not ob["mitigated"]
        and ob["price_low"] < current_price
        and _near_zone(current_price, ob["price_low"], ob["price_high"], "long", max_entry_zone_mult)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1]["price_low"], reverse=True)

    bias, is_choch, bias_ref = _structure_bias(features)
    fvgs = features.get("fvgs", [])
    sweeps = features.get("liquidity_sweeps", [])
    fresh_cutoff = n_bars - recent_window

    best: Setup | None = None
    for i, ob in candidates:
        points = W_ZONE
        factors = ["unmitigated bullish order block below price"]
        refs = [f"order_blocks:{i}"]

        if bias == "bullish":
            points += W_STRUCTURE
            factors.append("structure aligned (last break bullish)")
            if bias_ref:
                refs.append(bias_ref)
            if is_choch:
                points += W_CHOCH
                factors.append("...and it was a bullish CHoCH (reversal)")

        # Unfilled bullish FVG sitting between the OB and price, backing the move.
        for j, f in enumerate(fvgs):
            if (
                f["type"] == "fvg_bullish"
                and not f["mitigated"]
                and f["price_low"] < current_price
                and f["price_high"] >= ob["price_low"]
            ):
                points += W_FVG
                factors.append("unfilled bullish FVG backing the move")
                refs.append(f"fvgs:{j}")
                break

        # Recent sell-side liquidity sweep (stop-run below a low, then reject).
        for j, s in enumerate(sweeps):
            if s["type"] == "sweep_bullish" and s["sweep_index"] >= fresh_cutoff:
                points += W_SWEEP
                factors.append("recent sell-side liquidity sweep (bullish rejection)")
                refs.append(f"liquidity_sweeps:{j}")
                break

        if ob["end_index"] >= fresh_cutoff:
            points += W_FRESH
            factors.append("zone formed recently")

        cand = Setup(
            side="long",
            entry=current_price,
            stop=ob["price_low"],
            score=points / _MAX_POINTS,
            points=points,
            factors=factors,
            detector_refs=refs,
            anchor_ref=f"order_blocks:{i}",
        )
        if best is None or cand.points > best.points:
            best = cand
    return best


def _score_short(
    features: dict[str, list[dict[str, Any]]],
    current_price: float,
    *,
    n_bars: int,
    recent_window: int,
    max_entry_zone_mult: float | None,
) -> Setup | None:
    obs = features.get("order_blocks", [])
    candidates = [
        (i, ob)
        for i, ob in enumerate(obs)
        if ob["type"] == "ob_bearish"
        and not ob["mitigated"]
        and ob["price_high"] > current_price
        and _near_zone(current_price, ob["price_low"], ob["price_high"], "short", max_entry_zone_mult)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[1]["price_high"])  # nearest resistance first

    bias, is_choch, bias_ref = _structure_bias(features)
    fvgs = features.get("fvgs", [])
    sweeps = features.get("liquidity_sweeps", [])
    fresh_cutoff = n_bars - recent_window

    best: Setup | None = None
    for i, ob in candidates:
        points = W_ZONE
        factors = ["unmitigated bearish order block above price"]
        refs = [f"order_blocks:{i}"]

        if bias == "bearish":
            points += W_STRUCTURE
            factors.append("structure aligned (last break bearish)")
            if bias_ref:
                refs.append(bias_ref)
            if is_choch:
                points += W_CHOCH
                factors.append("...and it was a bearish CHoCH (reversal)")

        for j, f in enumerate(fvgs):
            if (
                f["type"] == "fvg_bearish"
                and not f["mitigated"]
                and f["price_high"] > current_price
                and f["price_low"] <= ob["price_high"]
            ):
                points += W_FVG
                factors.append("unfilled bearish FVG backing the move")
                refs.append(f"fvgs:{j}")
                break

        for j, s in enumerate(sweeps):
            if s["type"] == "sweep_bearish" and s["sweep_index"] >= fresh_cutoff:
                points += W_SWEEP
                factors.append("recent buy-side liquidity sweep (bearish rejection)")
                refs.append(f"liquidity_sweeps:{j}")
                break

        if ob["end_index"] >= fresh_cutoff:
            points += W_FRESH
            factors.append("zone formed recently")

        cand = Setup(
            side="short",
            entry=current_price,
            stop=ob["price_high"],
            score=points / _MAX_POINTS,
            points=points,
            factors=factors,
            detector_refs=refs,
            anchor_ref=f"order_blocks:{i}",
        )
        if best is None or cand.points > best.points:
            best = cand
    return best


def best_setup(
    features: dict[str, list[dict[str, Any]]],
    current_price: float,
    *,
    n_bars: int,
    recent_window: int = DEFAULT_RECENT_WINDOW,
    min_score: float = 0.0,
    max_entry_zone_mult: float | None = DEFAULT_MAX_ENTRY_ZONE_MULT,
) -> Setup | None:
    """Highest-confluence tradable setup at ``current_price``, or None.

    Evaluates both directions and returns the better-scoring one, provided it
    clears ``min_score`` and price is near enough to the zone not to be
    chasing (``max_entry_zone_mult``). ``n_bars`` is the number of closed bars
    the features were computed over — used only to judge recency, never to
    look ahead.
    """
    long = _score_long(
        features, current_price, n_bars=n_bars, recent_window=recent_window,
        max_entry_zone_mult=max_entry_zone_mult,
    )
    short = _score_short(
        features, current_price, n_bars=n_bars, recent_window=recent_window,
        max_entry_zone_mult=max_entry_zone_mult,
    )

    best = max(
        (s for s in (long, short) if s is not None),
        key=lambda s: s.points,
        default=None,
    )
    if best is None or best.score < min_score:
        return None
    return best


def score_for_refs(
    features: dict[str, list[dict[str, Any]]],
    current_price: float,
    side: str,
    *,
    n_bars: int,
    recent_window: int = DEFAULT_RECENT_WINDOW,
) -> float:
    """Confluence score the engine *would* assign to the best setup on ``side``.

    Used by the validator to gate a model-proposed trade: the model can cite
    whatever refs it likes, but if the deterministic confluence for that
    direction is weak, the trade is weak. Returns 0.0 if there's no valid zone.
    """
    if side == "long":
        s = _score_long(
            features, current_price, n_bars=n_bars, recent_window=recent_window,
            max_entry_zone_mult=None,  # validator scores the setup, not its entry timing
        )
    elif side == "short":
        s = _score_short(
            features, current_price, n_bars=n_bars, recent_window=recent_window,
            max_entry_zone_mult=None,
        )
    else:
        return 0.0
    return s.score if s is not None else 0.0
