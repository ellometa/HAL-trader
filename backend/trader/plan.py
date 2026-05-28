"""The ``TradePlan`` — the only thing the model is allowed to emit.

This is the contract between the LLM and the executor. The model does not
place orders, does not size positions in dollars, does not touch the
broker. It produces this object; everything downstream is deterministic
Python that can be unit-tested without an LLM in the loop.

Why a schema and not free-text tool calls (Fork A)? Because the failure
modes of an LLM trader — hallucinated setups, inverted risk/reward,
confidence inflation — are all *checkable* once the decision is a typed
object. A validator can reject a bad plan. It cannot reject a bad
paragraph.

The schema is intentionally Gemini-structured-output friendly: only
``Literal`` enums, ``Optional`` scalars, and a ``list[str]``. No nested
models, no unions beyond Optional. Keeping it flat means the same schema
round-trips through Gemini today and another vendor tomorrow without surgery.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Action = Literal["open_long", "open_short", "wait", "close_existing"]
Confidence = Literal["low", "medium", "high"]


class TradePlan(BaseModel):
    """A single decision for a single symbol at a single point in time.

    For ``wait`` and ``close_existing`` the price fields may be null — they
    only matter when opening. The validator (``risk.validate``) is what
    enforces that an ``open_*`` plan actually carries entry/stop/target and
    that those levels are internally consistent; this model only enforces
    shape.
    """

    action: Action = Field(
        description="What to do. 'wait' is always a legal, safe answer."
    )
    rationale: str = Field(
        description="Why, in one or two sentences. Logged verbatim to the journal."
    )
    entry: float | None = Field(
        default=None, description="Intended entry price (open_* only)."
    )
    stop: float | None = Field(
        default=None, description="Protective stop price (open_* only)."
    )
    take_profit: float | None = Field(
        default=None, description="Target price (open_* only)."
    )
    size_pct_equity: float | None = Field(
        default=None,
        description=(
            "Fraction of equity to deploy as notional, 0..1. The server "
            "clamps this; the model's number is a request, not a command."
        ),
    )
    validity_bars: int = Field(
        default=12,
        ge=1,
        description="Time-stop: close the position after this many closed bars.",
    )
    confidence: Confidence = "low"
    detector_refs: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of the detector events that justify this plan, e.g. "
            "'order_blocks:2'. Every ref MUST exist in the features the "
            "model was shown; the validator rejects phantom references."
        ),
    )
