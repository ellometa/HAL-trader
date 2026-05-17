# HAL — Progress Report

Snapshot of where the project stands. Updated as phases land.

---

## Build status

| Phase | Title                          | Status     | Commit     |
|-------|--------------------------------|------------|------------|
| 1     | Scaffolding + Gemini smoke     | ✅ done    | `292ba29`  |
| 2     | ICT detector (FVG + OB)        | ✅ done    | `0ab8035`  |
| 3     | OHLC fetcher (Binance + yf)    | ✅ done    | `2750f47`  |
| 4     | FastAPI `/analyze` endpoint    | ✅ done    | `bae5acb`  |
| 5     | Notion → notes.md ingestion    | ✅ done    | `dbf6537`  |
| 6     | Chrome extension MVP           | ✅ working, uncommitted |
| 7     | SSE streaming                  | ⏳ next    | —          |
| 8     | Polish (incl. Pine highlighter)| ⏳ planned | —          |

**6 of 8 v0 phases complete.** 5 committed, 1 (phase 6) verified working
and awaiting commit. All 8 phase prompts (+ overview) exist as
standalone markdowns in `plans/`. Future phases 9–14 sketched in
`plans/new_plans.md` (no standalone prompts yet).

---

## What works end-to-end today

1. User opens TradingView chart in Chrome → blue **HAL** button appears bottom-right.
2. Click → panel opens with symbol auto-detected from URL, timeframe picker (defaults 1h), persisted chat history per chart.
3. Type a question → POST to `localhost:8000/analyze`.
4. Backend fetches last 200 candles (Binance for crypto, yfinance otherwise) → runs deterministic FVG + OB detection → stuffs results + Notion-ingested trading notes into Gemini 2.5 Flash → returns answer.
5. Answer renders in the panel. Conversation persists via `chrome.storage.local`.

Verified live against BTCUSDT with real Notion notes (Module 6, OTE 70.5% sweet spot quoted verbatim).

---

## Test coverage

- **19 unit tests, all passing:**
  - 8 FVG cases (bullish/bearish detection + mitigation)
  - 5 OB cases (BOS detection, swing handling, mitigation gating)
  - 6 OHLC cases (Binance happy path, 4xx, empty data, yfinance schema, timeframe routing)
- No integration / extension tests (manual verification per phase 6 plan).

---

## Key decisions locked

- **Package manager:** uv. Pinning: compatible-release (`~=`). Python 3.13 chosen.
- **LLM:** Gemini 2.5 Flash via `google-genai` SDK (NEW, not `google-generativeai`). Async path via `client.aio.*`.
- **Notes ingestion:** `notion-client` SDK, full overwrite on each run (no incremental sync, no RAG).
- **Chart visualization:** Path 1 (standalone Pine Script indicators) chosen over path 3 (extension-driven Pine). Filed under phase 8 polish item P4b.
- **Conversation persistence:** `chrome.storage.local`, per-chart key, 50-message cap, manual clear.
- **Symbol detection:** URL `?symbol=…` is the source of truth (TV title doesn't carry timeframe). Timeframe is user-picked via dropdown.
- **CORS:** allows `chrome-extension://*`, `localhost`, and `https://(www.)?tradingview.com` (because MV3 content-script fetches use the page's origin).

---

## Known issues / debt

- **EURUSD heuristic:** crypto regex matches it → routes to Binance, where it fails. Filed under phase 8 polish; needs forex branch using yfinance `=X` suffix.
- **No streaming:** answer appears all at once after the full Gemini round-trip. Fixed in phase 7.
- **No abort:** can't cancel in-flight queries. Phase 8.
- **Notes reload:** requires uvicorn restart to pick up new `notes.md`. Phase 8 adds `/reload-notes`.
- **Phase 6 uncommitted.** Should commit before starting phase 7.
- **Local-only push:** all commits on `main` locally, last push was after phase 4. Phases 5 and 6 still need pushing to GitHub.

---

## Stack snapshot

```
backend/
  config.py          — .env loading, secret validation
  main.py            — FastAPI app, /analyze, /health, CORS
  ohlc.py            — Binance + yfinance routing
  prompts.py         — system/user prompt assembly
  hello.py           — phase 1 smoke test (kept for reference)
  ict/
    fvg.py           — Fair Value Gap detector
    order_blocks.py  — Order Block detector
    detector.py      — orchestrator: detect_all(df)
  notes.md           — Notion content, gitignored

scripts/
  ingest_notion.py   — Notion → backend/notes.md

extension/
  manifest.json      — MV3
  content.js         — UI + symbol detection + fetch + storage
  panel.css          — dark theme matching TV
  icons/             — placeholder PNGs

tests/               — pytest, 19 cases, all green
plans/               — 9 markdowns (overview + 1–8) + new_plans + this report
```

---

## Next actions

1. Commit phase 6 (`extension/` + the CORS fix in `backend/main.py`).
2. Push everything to GitHub (5 + 6 not yet pushed).
3. Start phase 7 — SSE streaming. Plan is in `plans/07_streaming.md`.
