"""Where a plan comes from. Two policies, same output type.

``llm``  — Gemini emits a ``TradePlan`` via structured output (a JSON schema
           derived from the Pydantic model). Structured output, not free
           text, is the whole game: it makes the decision a typed object the
           validator can reason about. The single model call is isolated in
           ``_generate_plan_json`` so swapping Gemini for another vendor later is a
           one-function change, not a rewrite.

``rule`` — a deterministic baseline that needs no API at all. It exists for
           two reasons: (1) doc 07's Phase A — prove the pipes end-to-end
           before trusting a model, and (2) a sanity floor the LLM policy can
           be compared against. If the model can't beat "long a fresh bullish
           OB after a bullish BOS," the model isn't earning its cost.

Both policies can only ever cite detector refs that exist; the validator
enforces it regardless, but the prompt also shows the model the exact
catalog of legal refs so it has no excuse to invent one.
"""
from __future__ import annotations

import json
from typing import Any

from backend.trader.confluence import best_setup
from backend.trader.config import RiskConfig
from backend.trader.plan import TradePlan
from backend.trader.risk import catalog_refs


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------
def build_decision_prompt(
    *,
    symbol: str,
    timeframe: str,
    current_price: float,
    features: dict[str, list[dict[str, Any]]],
    portfolio: dict[str, Any],
    notes: str,
    risk: RiskConfig,
) -> tuple[str, str]:
    """Return (system_prompt, user_message) for the decision call."""
    refs = catalog_refs(features)
    notes_block = notes.strip() or "<no notes ingested>"
    system = f"""You are HAL's execution brain for an AUTONOMOUS PAPER trading account.
You do not chat. You output exactly one TradePlan as JSON and nothing else.

Hard rules (a plan that breaks any of these will be rejected by a
deterministic validator downstream, so do not bother emitting one):
- You may ONLY reference detector events by the IDs listed under
  "Valid detector refs". Never invent a setup that isn't in the features.
- An open_long needs stop < entry < take_profit. An open_short needs
  take_profit < entry < stop.
- reward:risk must be at least {risk.min_rr}. Compute it from your own
  entry/stop/take_profit before committing.
- Risk per trade is capped server-side at {risk.max_risk_pct:.0%} of equity
  and position size at {risk.max_position_pct:.0%}. Your size_pct_equity is a
  request that will be clamped; never assume you got the size you asked for.
- "wait" is a first-class answer and usually the correct one. Only propose a
  trade when the geometry genuinely lines up with the notes below. There is
  no penalty for waiting and a real cost (fees + slippage) for churning.

Use the trading notes as the authoritative definition of a valid setup.
When notes and generic ICT disagree, the notes win.

--- NOTES START ---
{notes_block}
--- NOTES END ---
"""

    user = (
        f"Symbol: {symbol}   Timeframe: {timeframe}   "
        f"Current price: {current_price}\n"
        f"(All feature timestamps are UTC ISO 8601. Indices refer to closed candles.)\n\n"
        f"Account state:\n{json.dumps(portfolio, indent=2, default=str)}\n\n"
        f"Valid detector refs (cite ONLY these in detector_refs):\n{refs}\n\n"
        f"Detected features:\n{json.dumps(features, indent=2, default=str)}\n\n"
        f"Emit one TradePlan for {symbol} now."
    )
    return system, user


# --------------------------------------------------------------------------
# LLM policy
# --------------------------------------------------------------------------
async def _generate_plan_json(client: Any, model: str, system: str, user: str) -> str:
    """The single model touchpoint. Returns raw JSON text.

    Isolated so the rest of the system is model-agnostic: point this at
    another vendor's API instead of Gemini and nothing else changes.
    """
    from google.genai import types

    resp = await client.aio.models.generate_content(
        model=model,
        contents=user,
        config=types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=TradePlan,
            temperature=0.2,
        ),
    )
    # The SDK may hand back a parsed instance; prefer it, fall back to text.
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, TradePlan):
        return parsed.model_dump_json()
    return resp.text or "{}"


async def llm_plan(
    *,
    client: Any,
    model: str,
    symbol: str,
    timeframe: str,
    current_price: float,
    features: dict[str, list[dict[str, Any]]],
    portfolio: dict[str, Any],
    notes: str,
    risk: RiskConfig,
) -> tuple[TradePlan, str, str]:
    """Return (plan, system_prompt, user_message). The prompts are returned
    so the engine can snapshot them into the journal verbatim."""
    system, user = build_decision_prompt(
        symbol=symbol,
        timeframe=timeframe,
        current_price=current_price,
        features=features,
        portfolio=portfolio,
        notes=notes,
        risk=risk,
    )
    raw = await _generate_plan_json(client, model, system, user)
    try:
        plan = TradePlan.model_validate_json(raw)
    except Exception:
        # A malformed plan is not a reason to do something risky. Wait.
        plan = TradePlan(
            action="wait",
            rationale=f"model returned unparseable plan; defaulting to wait. raw={raw[:200]!r}",
        )
    return plan, system, user


# --------------------------------------------------------------------------
# Deterministic rule policy (no API)
# --------------------------------------------------------------------------
def _confidence_for(score: float) -> str:
    """Map a confluence score to a stated confidence, so the rule policy's
    confidence means something the calibration report can check."""
    if score >= 0.8:
        return "high"
    if score >= 0.6:
        return "medium"
    return "low"


def rule_plan(
    *,
    features: dict[str, list[dict[str, Any]]],
    current_price: float,
    risk: RiskConfig,
    position_side: str | None,
    validity_bars: int,
    n_bars: int,
) -> TradePlan:
    """Baseline policy: take the single highest-confluence setup available,
    but only if it clears the configured quality gate. No model involved.

    Two deliberate changes from the naive v0 baseline, both backed by the
    first backtest: (1) entries are chosen and gated by ``confluence`` rather
    than "first OB in the trend direction", which cuts marginal, fee-churning
    trades; (2) there is NO discretionary close on a structure flip — that
    logic was the single largest loss bucket in the backtest (it dumped
    positions mid-retrace near lows). Exits are left to the deterministic
    stop / target / time-stop the engine already enforces.
    """
    # One position per symbol: while holding, do nothing and let the
    # engine-side exits manage the trade.
    if position_side is not None:
        return TradePlan(action="wait", rationale="holding a position; exits are engine-managed")

    setup = best_setup(
        features, current_price, n_bars=n_bars, min_score=risk.min_confluence
    )
    if setup is None:
        return TradePlan(
            action="wait",
            rationale=f"no setup clears the confluence gate ({risk.min_confluence:.2f})",
        )

    rr = max(risk.min_rr, 2.0)
    if setup.side == "long":
        tp = setup.entry + rr * (setup.entry - setup.stop)
        action = "open_long"
    else:
        tp = setup.entry - rr * (setup.stop - setup.entry)
        action = "open_short"

    rationale = f"confluence {setup.score:.2f}: " + "; ".join(setup.factors)
    return TradePlan(
        action=action,
        rationale=rationale,
        entry=setup.entry,
        stop=setup.stop,
        take_profit=tp,
        size_pct_equity=risk.max_position_pct,
        validity_bars=validity_bars,
        confidence=_confidence_for(setup.score),
        detector_refs=setup.detector_refs,
    )
