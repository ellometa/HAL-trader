# Phase 8 — Detector depth + backend polish

## Goal

Make HAL's *answers* better. Phase 8 deepens the detector layer, caches
hot work, surfaces what HAL actually saw, and writes a README that
matches reality. Frontend polish lives in phase 9.

## Prerequisite context

Phases 1–7 done:
- Backend on `127.0.0.1:8000`, streaming SSE.
- Detectors: FVG + OB (no BOS/CHoCH, no liquidity sweeps yet).
- OHLC: Binance + yfinance, fetched per call, no cache.
- Extension: floating button + per-chart panel, history in
  `chrome.storage.local`.

Read before starting: `plans/00_overview.md`, `plans/02_ict_detector.md`,
`backend/ict/fvg.py`, `backend/ict/order_blocks.py`, `backend/main.py`,
`backend/prompts.py`.

---

## Scope (in order)

### P1. Structure (BOS/CHoCH) + liquidity sweep detectors

The single biggest lift for answer quality. Trend questions without
structure are guesswork.

**Files to add**
- `backend/ict/structure.py` — BOS (Break of Structure) and CHoCH
  (Change of Character) detection.
- `backend/ict/liquidity.py` — Equal highs/lows + sweep detection
  (wick beyond a recent swing followed by close back inside).
- `tests/test_structure.py`, `tests/test_liquidity.py` — fixture-based.

**Refactor**
- Move `_find_swings` from `order_blocks.py` to `structure.py`.
- `order_blocks.py` imports it.

**Wire-up**
- Add `detect_structure(df, swing_n)` and `detect_liquidity(df, swing_n)`
  to `detect_all` in whatever module composes the detector pipeline.
- Update the features schema in `prompts.py` so the new keys appear in
  the user message.

**Definitions to pin down before coding**
- BOS: most recent confirmed swing-high broken by close above (bullish)
  or swing-low broken by close below (bearish).
- CHoCH: BOS in the opposite direction of the prior trend leg.
- Liquidity sweep: wick of bar N pokes beyond swing high/low formed in
  the prior `swing_n` bars, and bar N closes back below/above that
  level.

**Acceptance**
- `pytest tests/test_structure.py tests/test_liquidity.py` green.
- `/analyze` response on a known chart includes structure + liquidity
  in the features payload.
- Manually verified: ask HAL "what's the structure?" on BTC 4h, it
  cites BOS/CHoCH terms rather than hedging.

### P7. OHLC caching

In-memory LRU keyed by `(symbol, timeframe, current_minute_unix)`.

- ~20 lines using `functools.lru_cache` won't work directly (no TTL);
  hand-roll a dict + timestamp eviction OR use `cachetools.TTLCache`.
- TTL = 60 seconds. Back-to-back queries on the same chart hit cache.
- Cache key includes the *minute*, not the second, so cache invalidates
  on the natural bar boundary.

**Bonus**
- Also cache *features*. Detection is deterministic over OHLC, so
  `(symbol, timeframe, df_hash)` → features is free to memoize.
- Skip if it complicates P1's wiring. Do this only after P1 is solid.

**Acceptance**
- Two `/analyze` calls within 60s for the same `(symbol, tf)` produce
  one yfinance/Binance hit. Verify with a print or counter.

### P6. Debug expander in the panel

`meta` SSE event already carries detected features. Surface them.

**Extension changes**
- After the assistant bubble renders, append a `<details>` element
  with a one-line summary ("3 FVGs · 1 OB · BOS bullish") and the
  full JSON inside a `<pre>` on expand.
- Reuse the `meta.features` already stashed in `appendMessage`.
- Style minimally: same monospace font, slightly dimmer than the
  message text.

**Acceptance**
- Each assistant message has a collapsed "View detected features"
  toggle. Open it, see the structured payload HAL reasoned over.

### P15. README that matches phase-7 reality

`README.md` is still the phase-1 bootstrap. Rewrite.

**Sections**
1. 60-second overview — what HAL is, what it isn't.
2. Architecture diagram (ASCII or steal/inline the one from
   `plans/00_overview.md`).
3. Install — uv, env vars (`GEMINI_API_KEY`, `NOTION_TOKEN`,
   `NOTION_ROOT_PAGE_ID`), `uv run uvicorn …`, extension load
   unpacked.
4. Update notes — `uv run python scripts/ingest_notion.py`. Note that
   ingestion overwrites `backend/notes.md`.
5. Troubleshooting:
   - TV symbol detection failures (until phase 9 P4 lands)
   - yfinance flakiness (refer to P8 errors once they exist)
   - CORS (allow-list, extension reload, etc.)
   - "Backend not running" — how to tell, how to start.

Keep it short. ~150–250 lines. No fluff.

**Acceptance**
- A second person could install and run HAL from the README alone.
- Trade jargon is either defined inline or linked.

---

## Out of scope for phase 8

- Extension UX (abort, thinking state, shadow DOM, symbol detection
  hardening) — phase 9.
- Conversation memory — phase 9.
- Pine Script highlighter — phase 9.
- Detector toggles (P13), eval harness (P14), prompt tuning loop
  (P10) — post-v0.
- Journaling, SQLite, server-side history — separate plan after v0.

## Suggested order

1. P1 first. It's the biggest and unlocks better answers immediately.
2. P7 second. Easy, makes P1 testing faster.
3. P6 third. Trivial once meta event is wired (it already is).
4. P15 last. Write the README *after* the new detectors so you don't
   rewrite it twice.

## Definition of done

- All four items shipped, committed, pushed.
- Manual smoke test: open BTC 4h chart, ask 3 different question types
  (structure, FVG-specific, free-form), debug expander confirms HAL
  saw what you expected.
- README would actually onboard someone.
