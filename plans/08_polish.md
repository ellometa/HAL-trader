# Phase 8 — Polish (prioritized list, not a single execution)

## Goal

The end-to-end loop works. Now we make it pleasant and robust. Each
item below is a self-contained mini-task; pick whichever the user
finds most painful in daily use. They are listed roughly in
priority order, but the user's actual usage friction should re-rank
them.

## Context

Everything from phases 1–7 exists and works:
- Backend on localhost:8000, streaming SSE
- ICT detectors (FVG, OB; possibly structure + liquidity)
- OHLC fetcher (Binance + yfinance)
- Notion ingestion → `notes.md`
- Chrome extension with floating button and streaming panel

This phase doesn't add new features; it sharpens existing ones.

---

## Tier 1 — fixes that affect every interaction

### P1. Add structure (BOS/CHoCH) and liquidity sweeps to the detector

If they were punted from phase 2 (likely), add them now. The Gemini
answers without market structure are noticeably weaker for any
trend-related question.

- New files: `backend/ict/structure.py`, `backend/ict/liquidity.py`
- New tests: `tests/test_structure.py`, `tests/test_liquidity.py`
- Wire into `detect_all`.
- The shared `_find_swings` helper can move from `order_blocks.py`
  into `structure.py` and be imported by OB.

### P2. Conversation memory (per-tab, in-memory)

Currently every `/analyze` call is independent. Add a `session_id`
to requests and let the backend keep the last few turns in a dict
keyed by session id. Extension generates a UUID on panel open.

- Trade-off: dict grows. Cap to last N turns per session, evict
  sessions inactive >1h.
- The system prompt grows with each turn — be mindful of context
  cost. Gemini Flash is cheap, but still.

### P3. Visible "thinking" state and an Abort button

`AbortController` on the fetch. Stop button while streaming.

### P4. Robust symbol detection on TradingView

The `document.title` regex breaks on some symbol formats (e.g.
indices, futures with month codes). Add tests by manually visiting
5+ chart types and recording the parsed result.

---

### P4b. Pine Script FVG / OB highlighter (visual rail)

Standalone Pine v6 indicators that mirror `backend/ict/fvg.py` and
`backend/ict/order_blocks.py` so the user sees the same zones HAL
reasons about, drawn natively on the TradingView chart. Independent
of the extension — paste-and-add-to-chart, one-time install. Detection
rule MUST be identical to the Python implementation so numbers line up.
Path 3 (extension-driven Pine) explicitly out of scope — too brittle
for the maintenance cost. Two scripts:
- `pine/hal_fvg.pine` — bullish/bearish/mitigated toggles.
- `pine/hal_ob.pine` — bullish/bearish/mitigated toggles, swing_n input.

## Tier 2 — quality wins

### P5. `/reload-notes` endpoint

POST endpoint that re-reads `notes.md` without restarting uvicorn.
Useful right after running the ingest script.

### P6. Debug expander in the panel

The `meta` event already carries detected features. Render them as a
collapsed accordion under each assistant message: "View detected
features (3 FVGs, 1 OB)…"

### P7. OHLC caching

In-memory cache keyed by `(symbol, timeframe, current_minute)`. Cuts
back-to-back queries on the same chart to a single fetch.

### P8. yfinance error surfaces

yfinance is flaky — when it returns empty, surface a useful error
("No data for AAPL 1m — yfinance only retains ~7 days of 1m data").

### P9. Shadow DOM in the extension

If TradingView styles bleed into the panel (or vice versa), wrap the
UI in a Shadow DOM root.

---

## Tier 3 — nice to have

### P10. Prompt tuning loop

`?debug=1` already returns the assembled prompt. Build a tiny script
that lets the user iterate on `prompts.py` against a saved
`(symbol, timeframe, query)` fixture.

### P11. ~~Persist conversation across reloads~~ (LANDED IN PHASE 6)

Already in v0 — `chrome.storage.local` keyed per chart, capped at 50
messages, with a "Clear chat" button. Polish item: add cross-device
sync via `chrome.storage.sync` if it ever matters (probably never).

### P12. Multiple-chart awareness

If the user has 2 TradingView tabs open, each maintains its own
session and panel state cleanly. Largely free once P2 lands.

### P13. Tradeoff variants in the detector

Phase 2 noted alternative FVG and OB definitions. Add toggles:
- `FVG_REQUIRE_IMPULSIVE_MIDDLE=True/False`
- `OB_REQUIRE_FVG_CONFIRMATION=True/False`
Document expected effect on detection counts.

### P14. Tiny eval harness

Hand-craft 5–10 (chart, question, expected-shape-of-answer) cases.
Run them after any prompt or detector change.

### P15. README that doesn't suck

The phase 1 README is just bootstrap. Replace with:
- 60-second overview
- Architecture diagram (steal the one from `00_overview.md`)
- How to run
- How to update notes
- Troubleshooting (TV symbol detection, yfinance flakiness, CORS)

### P16. Better icon

The placeholder is a colored square. Make it look like HAL 9000's
red eye. (Mandatory only if vibes-driven dev is enforced.)

---

## How to execute a polish item

Each item above is small enough to be its own session. The skeleton
prompt for any of them:

```
HAL polish task: <item label, e.g. "P5. /reload-notes endpoint">

Context: phases 1–7 are complete and working. See plans/00_overview.md
for architecture. Files relevant to this task: <list>.

Do: <restate the item description>

Acceptance: <restate or derive>
```

## Out of scope (for the entire phase, not just one item)

- No deployment. Still localhost.
- No multi-user auth.
- No paid data sources.
- No mobile / Safari extension.
- No fine-tuning, no embeddings, no RAG. (Those are post-v0 ideas,
  not polish.)
- No rewriting in TypeScript / React. Vanilla JS stays.
