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
def _last_structure_bias(features: dict[str, list[dict[str, Any]]]) -> str | None:
    structure = features.get("structure", [])
    if not structure:
        return None
    last = structure[-1]["type"]
    if last.endswith("bullish"):
        return "bullish"
    if last.endswith("bearish"):
        return "bearish"
    return None


def rule_plan(
    *,
    features: dict[str, list[dict[str, Any]]],
    current_price: float,
    risk: RiskConfig,
    position_side: str | None,
    validity_bars: int,
) -> TradePlan:
    """Phase-A baseline: trade a fresh, unmitigated order block in the
    direction of the most recent structure break. No model involved."""
    bias = _last_structure_bias(features)

    # Discretionary exit: if we hold a position and structure has flipped
    # against it, close. (Stops/targets/time-stops are handled by the engine.)
    if position_side == "long" and bias == "bearish":
        return TradePlan(action="close_existing", rationale="structure flipped bearish against long")
    if position_side == "short" and bias == "bullish":
        return TradePlan(action="close_existing", rationale="structure flipped bullish against short")
    if position_side is not None:
        return TradePlan(action="wait", rationale="already in a position; no flip signal")

    obs = features.get("order_blocks", [])
    rr = max(risk.min_rr, 2.0)

    if bias == "bullish":
        for i, ob in enumerate(obs):
            if ob["type"] == "ob_bullish" and not ob["mitigated"] and ob["price_low"] < current_price:
                entry = current_price
                stop = ob["price_low"]
                if stop >= entry:
                    continue
                tp = entry + rr * (entry - stop)
                return TradePlan(
                    action="open_long",
                    rationale="bullish BOS with a fresh unmitigated bullish OB below price",
                    entry=entry, stop=stop, take_profit=tp,
                    size_pct_equity=risk.max_position_pct,
                    validity_bars=validity_bars,
                    confidence="medium",
                    detector_refs=[f"order_blocks:{i}"],
                )
    elif bias == "bearish":
        for i, ob in enumerate(obs):
            if ob["type"] == "ob_bearish" and not ob["mitigated"] and ob["price_high"] > current_price:
                entry = current_price
                stop = ob["price_high"]
                if stop <= entry:
                    continue
                tp = entry - rr * (stop - entry)
                return TradePlan(
                    action="open_short",
                    rationale="bearish BOS with a fresh unmitigated bearish OB above price",
                    entry=entry, stop=stop, take_profit=tp,
                    size_pct_equity=risk.max_position_pct,
                    validity_bars=validity_bars,
                    confidence="medium",
                    detector_refs=[f"order_blocks:{i}"],
                )

    return TradePlan(action="wait", rationale="no qualifying order block in the direction of structure")
