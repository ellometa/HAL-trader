"""Top-level ICT detector orchestrator. Bundles per-concept detectors
into a single dict so the backend can pass the lot to Gemini."""
from typing import Any

import pandas as pd

from backend.ict.fvg import detect_fvgs
from backend.ict.order_blocks import detect_order_blocks


def detect_all(df: pd.DataFrame) -> dict[str, list[dict[str, Any]]]:
    """Run all v0 detectors. Phase 8 will add structure (BOS/CHoCH) and
    liquidity sweeps under additional keys."""
    return {
        "fvgs": detect_fvgs(df),
        "order_blocks": detect_order_blocks(df),
    }
