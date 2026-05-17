"""System / user message construction for the /analyze endpoint.

Kept in one file so iterating on prompt wording doesn't touch routing
code. Tradeoff: alternative is Gemini's structured-output / function-
calling API. We chose plain text in / plain text out because (a) Flash
handles JSON-in-prompt fine, (b) the assembled prompt is trivially
copy-pasteable for debugging, (c) we want natural language back, not
structured data.
"""
import json
from datetime import datetime, timedelta, timezone
from typing import Any

# Display timezone for all times shown to the user. Detector internals stay
# in UTC; conversion happens here at the prompt boundary.
_IST = timezone(timedelta(hours=5, minutes=30))
_TIME_KEYS = ("start_time", "end_time", "time", "timestamp")


def _to_ist(value: Any) -> Any:
    """Recursively rewrite ISO datetime strings under known time keys to IST."""
    if isinstance(value, dict):
        return {k: _convert_field(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_ist(v) for v in value]
    return value


def _convert_field(key: str, value: Any) -> Any:
    if key in _TIME_KEYS and isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_IST).strftime("%Y-%m-%d %H:%M IST")
    return _to_ist(value)


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

All timestamps in the features payload are already in IST (Asia/Kolkata,
GMT+5:30). Always refer to times in IST. Do not convert to UTC and do not
mention UTC. The "IST" suffix is the canonical label.

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
    features_ist = _to_ist(features)
    return (
        f"Symbol: {symbol}   Timeframe: {timeframe}   "
        f"Current price: {current_price}\n\n"
        f"Detected features (last 200 candles, times in IST / GMT+5:30):\n"
        f"{json.dumps(features_ist, indent=2, default=str)}\n\n"
        f"Question: {query}\n"
    )
