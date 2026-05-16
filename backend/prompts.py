"""System / user message construction for the /analyze endpoint.

Kept in one file so iterating on prompt wording doesn't touch routing
code. Tradeoff: alternative is Gemini's structured-output / function-
calling API. We chose plain text in / plain text out because (a) Flash
handles JSON-in-prompt fine, (b) the assembled prompt is trivially
copy-pasteable for debugging, (c) we want natural language back, not
structured data.
"""
import json
from typing import Any


def build_system_prompt(notes: str) -> str:
    """Inject trading notes into a fixed instruction frame.

    Empty notes get a visible placeholder so the model still understands
    the structure exists; this happens before phase 5 has ingested anything.
    """
    notes_block = notes.strip() or "<no notes ingested yet>"
    return f"""You are HAL, a trading-chart analyst that uses ICT (Inner Circle Trader) concepts.

You receive structured, deterministically-detected chart features as JSON.
Do NOT invent features that aren't in the JSON. If the user's question
cannot be answered from the supplied features, say so plainly.

Use the trading notes below as authoritative context for how the user
thinks about setups, what they consider valid signals, and their personal
rules. When notes and standard ICT definitions disagree, prefer the notes.

--- NOTES START ---
{notes_block}
--- NOTES END ---
"""


def build_user_message(
    symbol: str,
    timeframe: str,
    current_price: float,
    features: dict[str, Any],
    query: str,
) -> str:
    return (
        f"Symbol: {symbol}   Timeframe: {timeframe}   "
        f"Current price: {current_price}\n\n"
        f"Detected features (last 200 candles):\n"
        f"{json.dumps(features, indent=2, default=str)}\n\n"
        f"Question: {query}\n"
    )
