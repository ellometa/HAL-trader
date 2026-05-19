# HAL

> "Long the OB at 2840, HAL."
> "I'm sorry, Dave. That's not a valid setup."

A chart copilot for TradingView. Personal tool, runs locally.

You open a chart and a question forms. *Is there a bullish order block
near current price? Did the 4h sweep yesterday's high before the move?
Where is the next pool of liquidity sitting?* HAL is the floating panel
you ask.

It doesn't look at the chart. It reads the underlying OHLC, runs a
handful of deterministic ICT detectors over the geometry — fair value
gaps, order blocks, market structure, liquidity sweeps — folds the
output into a prompt alongside your own trading notes, and streams a
Gemini 2.5 Flash answer back into the panel.

No vision model, no ML for detection. The geometry is computed in
Python and handed to the model as JSON. The model writes the prose;
the detectors are the ground truth.

## Architecture

```
  ┌──────────────────────────┐
  │ TradingView tab (Chrome) │
  │  ┌────────────────────┐  │
  │  │ HAL panel overlay  │  │  reads ?symbol= from URL
  │  │  - floating button │  │  user types question
  │  │  - SSE chat stream │  │
  │  └─────────┬──────────┘  │
  └────────────┼─────────────┘
               │ POST /analyze {symbol, timeframe, query}
               ▼
  ┌──────────────────────────────────────────┐
  │ FastAPI backend (127.0.0.1:8000)         │
  │                                          │
  │  ohlc.py  ─►  Binance / yfinance (cached)│
  │     │                                    │
  │     ▼                                    │
  │  ict/*  ─►  {fvgs, order_blocks,         │
  │     │        structure, liquidity_sweeps}│
  │     ▼                                    │
  │  prompts.py  ◄── notes.md (from Notion)  │
  │     │                                    │
  │     ▼                                    │
  │  Gemini 2.5 Flash (streaming)            │
  └────────────┬─────────────────────────────┘
               │ SSE: meta / token / done
               ▼
  Extension renders tokens progressively
```

## Install

### 1. Backend

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

```bash
brew install uv         # or pipx install uv / pip install uv
uv sync                 # creates .venv, installs deps
cp .env.example .env    # then edit .env
```

Set in `.env`:

```
GEMINI_API_KEY=...           # https://aistudio.google.com/apikey
NOTION_TOKEN=...             # only needed to re-ingest notes
NOTION_ROOT_PAGE_ID=...      # root page of your trading-notes tree
```

Run the server:

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl http://127.0.0.1:8000/health
# {"ok":true}
```

### 2. Chrome extension

1. Open `chrome://extensions`.
2. Enable Developer mode (top right).
3. Click "Load unpacked", point at the `extension/` directory.
4. Open any TradingView chart. A blue "HAL" button appears bottom-right.
5. Click it; type a question; press Enter.

The extension talks to `http://localhost:8000`. If the backend isn't
running, the panel surfaces an error.

## Updating notes

`backend/notes.md` is what HAL reasons over. Re-ingest from Notion:

```bash
uv run python scripts/ingest_notion.py
```

The script walks the Notion subtree rooted at `NOTION_ROOT_PAGE_ID`
and overwrites `backend/notes.md` with the flattened markdown. Restart
uvicorn to pick up changes (P5 reload endpoint is coming).

## Querying from curl (debugging)

The non-streaming `/analyze_blocking` endpoint returns the same shape as
`/analyze` but as a single JSON blob:

```bash
curl -s -X POST http://127.0.0.1:8000/analyze_blocking \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTCUSDT","timeframe":"4h","query":"What is the structure?"}' \
  | jq .
```

Add `?debug=1` to either endpoint to get the assembled system prompt +
user message echoed back. SSE endpoint:

```bash
curl -N -X POST http://127.0.0.1:8000/analyze \
  -H 'Content-Type: application/json' \
  -d '{"symbol":"BTCUSDT","timeframe":"4h","query":"What is the structure?"}'
```

## Troubleshooting

**"Couldn't detect a symbol on this page."** The extension reads
`?symbol=` from the chart URL. If you're on the homepage or a non-chart
page, this fails. Open a `/chart/...` URL and it'll work.

**`yfinance returned no data for X interval=1m period=5d`.** yfinance
throttles intraday history aggressively — 1m bars only go back ~7 days
and the API gets flaky under bursty calls. Try a higher timeframe; if
you still get nothing, wait a minute and retry.

**Forex symbols routing to Binance.** Known v0 limitation: `EURUSD`
matches the crypto regex (`[A-Z]+USD`) and goes to Binance, which
doesn't list it. Fix coming with the forex `=X` suffix routing.

**Panel covers a chart control.** The panel pins to the top-right
corner. Drag the chart's own toolbars first, or close the panel with ✕
when you don't need it.

**CORS error in the browser console.** The backend allows
`tradingview.com` + `localhost` origins. If you're on a different
TV mirror or a non-default extension origin, edit the
`allow_origin_regex` in `backend/main.py`.

**Backend not responding.** Is uvicorn actually up? `curl
http://127.0.0.1:8000/health` from a terminal. If it hangs, the port
might be held by an old process — `lsof -iTCP:8000 -sTCP:LISTEN` and
kill the stale pid.

## Repo layout

```
HAL/
  backend/
    main.py            FastAPI app, /analyze (SSE) + /analyze_blocking
    config.py          loads .env
    ohlc.py            Binance + yfinance fetch, 60s cache
    prompts.py         system + user prompt assembly, UTC→IST conversion
    notes.md           generated by scripts/ingest_notion.py
    ict/
      detector.py      composes per-concept detectors
      fvg.py           3-candle Fair Value Gaps
      order_blocks.py  BOS-confirmed order blocks
      structure.py     BOS / CHoCH + shared find_swings helper
      liquidity.py     wick-only sweeps of swing highs/lows
  extension/
    manifest.json
    content.js         injects FAB + panel into TV pages
    panel.css
    icons/
  scripts/
    ingest_notion.py   Notion subtree → notes.md
  tests/               pytest fixtures for detectors + OHLC
  plans/               phase plans, design docs, ideation
  pyproject.toml       uv-managed
```

## Status

Phases 1–9 have landed. Detection covers FVG, order blocks, BOS/CHoCH,
and liquidity sweeps. The panel lives inside a Shadow DOM, streams
tokens as they arrive, remembers conversation per-chart, parses the
timeframe from your question, and can be aborted mid-stream. OHLC is
cached for 60s per (symbol, timeframe). Pine highlighters in `pine/`
mirror the Python detection so you can audit what HAL claims against
your own eyes.

What comes after lives in `plans/ideation/` — journaling, launchd
auto-start, brain-topology shifts. Some of it will happen.

## Tech

Python 3.11+, FastAPI, `google-genai`, pandas, yfinance, httpx,
notion-client. Extension is vanilla JS — no bundler, no framework.
Package management via `uv`.
