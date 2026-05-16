# Phase 2 — ICT Detector

## Goal

Pure functions over an OHLC array that return structured ICT features
as JSON-serializable dicts. No network, no I/O, no global state.
Unit tests on hand-crafted candle arrays prove correctness for each
detector. Start with FVG and Order Blocks; structure (BOS/CHoCH) and
liquidity sweeps come at the end of this phase if time allows, or are
explicitly punted to a follow-up.

## Context (what exists going in)

Phase 1 produced:

- `backend/config.py`            (loads `.env`)
- `backend/hello.py`             (Gemini smoke test, unrelated)
- `backend/__init__.py`          (empty)
- `backend/ict/__init__.py`      (empty)
- `tests/__init__.py`            (empty)
- `requirements.txt`             (has `pandas`, `pytest`)

Nothing else relevant exists. There is no OHLC fetcher yet — this
phase must accept OHLC as a plain input parameter (DataFrame or list
of dicts) and not call any data source.

## Input contract

Detectors accept a `pandas.DataFrame` indexed by integer position (0..N-1)
with columns: `timestamp` (UTC datetime), `open`, `high`, `low`,
`close`, `volume` (float). Order: oldest → newest. The detector files
should validate columns at the top of each public function.

## Output contract

Each detector returns a list of dicts. Common fields:

```python
{
    "type": "fvg_bullish" | "fvg_bearish" | "ob_bullish" | "ob_bearish",
    "start_index": int,      # candle index where pattern begins
    "end_index": int,        # candle index where pattern is confirmed
    "start_time": str,       # ISO 8601
    "end_time": str,
    "price_high": float,     # top of the zone
    "price_low": float,      # bottom of the zone
    "mitigated": bool,       # has price returned into this zone since?
    "meta": { ... }          # detector-specific extras
}
```

Returning a list of dicts (not a DataFrame, not a custom class) makes
JSON serialization trivial for the backend.

## Deliverables

- `backend/ict/fvg.py`            — `detect_fvgs(df) -> list[dict]`
- `backend/ict/order_blocks.py`   — `detect_order_blocks(df) -> list[dict]`
- `backend/ict/detector.py`       — `detect_all(df) -> dict` orchestrator
                                    returning `{"fvgs": [...], "order_blocks": [...]}`
- `tests/test_fvg.py`             — hand-crafted candle arrays, both
                                    bullish and bearish, plus a no-FVG
                                    case and a mitigated-FVG case
- `tests/test_order_blocks.py`    — similar

(Structure + liquidity are NOT in this phase — see locked decisions
below. Phase 8 polish picks them up.)

## Detection rules (lock these in before coding)

### FVG (3-candle imbalance)

For candles at indices `i-2, i-1, i`:

- **Bullish FVG**: `low[i] > high[i-2]`. The zone is
  `(price_low=high[i-2], price_high=low[i])`.
- **Bearish FVG**: `high[i] < low[i-2]`. The zone is
  `(price_low=high[i], price_high=low[i-2])`.

`start_index = i-2`, `end_index = i`. `mitigated = True` if any later
candle's wick re-enters the zone.

**Tradeoff comment to include in `fvg.py`:** some ICT variants require
candle `i-1` to be impulsive (large body). We're using the pure
geometric definition (only wicks of candle 1 and 3 matter). This is
the most common formalization and produces more candidates; tighten
with a body-size filter later if signal:noise is bad.

### Order Block (last opposing candle before BOS)

1. Identify swing highs/lows with N=2 (a candle whose high/low exceeds
   the 2 candles on either side).
2. A **Break of Structure** happens at candle `i` when `close[i] >`
   the most recent unbroken swing high (bullish BOS), or
   `close[i] <` the most recent unbroken swing low (bearish BOS).
3. The **bullish OB** is the *last down candle* (`close < open`)
   before the impulsive up-move that caused the bullish BOS. Walk
   backwards from the BOS candle to find it. Zone:
   `(price_low=low, price_high=high)` of that down candle.
4. Mirror for bearish OB.

`mitigated` = True if any later candle's wick enters the OB zone after
formation.

**Tradeoff comment:** alternative definitions use the open/close range
instead of the full wick range; some practitioners require the OB
candle to be followed by an FVG. We're using the simplest wick-based
zone with no FVG-confirmation requirement. Note the choice — strict
"OB + FVG confirmation" is a high-precision variant worth A/Bing
later.

## Step-by-step prompt

You are implementing a deterministic ICT pattern detector in Python
3.11+. Working dir `/Users/ellometa/code/Experimenting/HAL`. Phase 1
scaffolding is done; `backend/ict/` exists but is empty except for
`__init__.py`.

1. Read this entire file before writing code. The detection rules are
   the contract — code to them exactly.
2. Implement `backend/ict/fvg.py` first. Single public function
   `detect_fvgs(df: pd.DataFrame) -> list[dict]`. Validate columns at
   entry. Iterate from `i=2` to `len(df)-1`. Compute mitigation in a
   second pass (don't nest loops inside the main scan if you can
   avoid it; or just accept O(N²) for now — N=200, doesn't matter).
3. Write `tests/test_fvg.py`. Required cases:
   - Empty DataFrame → empty list
   - DataFrame with < 3 rows → empty list
   - One clean bullish FVG → exactly 1 entry, `type="fvg_bullish"`,
     correct `price_low`/`price_high`
   - One clean bearish FVG → mirror
   - No-FVG case (overlapping wicks) → empty list
   - Mitigated FVG: bullish FVG followed by a candle that wicks back
     into the zone → `mitigated=True`
   Construct DataFrames inline with `pd.DataFrame({...})`. Don't read
   files.
4. Run `uv run pytest tests/test_fvg.py -v` and confirm all pass.
5. Implement `backend/ict/order_blocks.py`. Add a private
   `_find_swings(df, n=2)` helper (it'll be reused by structure.py
   later, but keep it private to this file for now — extract when
   structure.py needs it). Implement `detect_order_blocks(df)` per
   the rules above.
6. Write `tests/test_order_blocks.py`. Required cases:
   - A clear bullish OB: down candle, then 3 strong up candles,
     final close breaks a prior swing high → detector returns the
     down candle as the OB.
   - Bearish mirror.
   - No-BOS case → empty list.
   - Mitigated OB.
7. Run all tests with `uv run pytest -v`.
8. Implement `backend/ict/detector.py` with `detect_all(df) -> dict`
   that just calls the two detectors and bundles results.
9. STOP. Show the user `pytest` output and a sample `detect_all`
   output on a synthetic 50-candle DataFrame.

## Acceptance criteria

- `uv run pytest tests/ -v` → all green, at least 8 tests passing.
- `python -c "import pandas as pd; from backend.ict.detector import detect_all; ..."`
  on a synthetic DataFrame returns a dict with `fvgs` and
  `order_blocks` keys, both lists of dicts matching the output
  contract.
- All public functions have a short docstring stating input shape and
  output shape. No long prose.
- Every non-obvious rule choice has a one-line "why" comment.
- Code runs without hitting the network. `grep -r "requests\|httpx\|yfinance" backend/ict/`
  returns nothing.

## Locked decisions

- **Swing N = 2.**
- **Mitigated zones are returned with `mitigated: true`**, not
  filtered. Let Gemini decide relevance.
- **Structure (BOS/CHoCH) and liquidity sweeps are PUNTED to phase 8
  polish.** Phase 2 ships FVG + OB only.

## Out of scope

- No network calls. No data fetching.
- No FastAPI integration — phase 4 wires this up.
- No LLM calls.
- No performance optimization. N=200 candles; readability wins.
- No plotting / no matplotlib.
- Do not introduce a class hierarchy. Functions returning dicts.
