"""Top-level ICT detector orchestrator. Bundles per-concept detectors
into a single dict so the backend can pass the lot to Gemini."""
from typing import Any

import pandas as pd

from backend.ict.fvg import detect_fvgs
from backend.ict.liquidity import detect_liquidity_sweeps
from backend.ict.order_blocks import detect_order_blocks
from backend.ict.structure import detect_structure


def detect_all(df: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    """Run all detectors. Order is presentational only — each is independent."""
    return {
        "fvgs": detect_fvgs(df),
        "order_blocks": detect_order_blocks(df),
        "structure": detect_structure(df),
        "liquidity_sweeps": detect_liquidity_sweeps(df),
    }
