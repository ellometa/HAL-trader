# Phase 4 — Backend `/analyze` Endpoint (non-streaming first)

## Goal

A FastAPI server on `localhost:8000` exposing `POST /analyze`. It
accepts `{symbol, timeframe, query}`, fetches OHLC, runs the ICT
detector, builds a prompt, calls Gemini 2.5 Flash, and returns the
text response. Non-streaming for this phase — streaming is phase 7.
Validatable end-to-end with `curl`. No Chrome extension yet.

## Context (what exists going in)

- `backend/config.py`             (env loading, `GEMINI_API_KEY` available)
- `backend/ict/detector.py`       (`detect_all(df) -> dict`)
- `backend/ohlc.py`               (`async fetch_ohlc(symbol, timeframe, limit=200) -> pd.DataFrame`)
- `requirements.txt` has `fastapi`, `uvicorn`, `google-genai`

Notes ingestion (phase 5) doesn't exist yet. Use a hardcoded
placeholder `notes.md` content (or an empty string with a TODO) so
the system prompt has the right *shape* — phase 5 just swaps in real
content.

## Deliverables

- `backend/main.py`               — FastAPI app, `/analyze` POST, plus a
                                    `/health` GET for sanity. CORS open
                                    to `chrome-extension://*` only.
- `backend/prompts.py`            — `build_system_prompt(notes: str) -> str`
                                    and `build_user_message(symbol, timeframe, features, query) -> str`.
                                    Keep prompts in one file so iterating
                                    on them doesn't touch routing code.
- (Optional) `backend/notes.md`   — placeholder content if it doesn't
                                    exist; the loader should handle the
                                    missing-file case gracefully.

## Request / response contract

```
POST /analyze
Content-Type: application/json

{
  "symbol": "BTCUSDT",
  "timeframe": "1h",
  "query": "Are there any unmitigated bullish FVGs near current price?"
}

→ 200 OK
{
  "answer": "...Gemini's text...",
  "features": { "fvgs": [...], "order_blocks": [...] },
  "candles_used": 200,
  "current_price": 64321.5
}
```

Including `features` and `current_price` in the response is useful for
debugging — the extension UI can hide them. Phase 7 will replace the
response body with a stream, but the JSON shape is what the curl-based
testing of this phase verifies.

## Prompt design

**System prompt skeleton (`build_system_prompt`):**

```
You are HAL, a trading-chart analyst that uses ICT (Inner Circle Trader)
concepts. You receive structured, deterministically-detected chart
features as JSON. Do NOT invent features that aren't in the JSON.
If the user's question can't be answered from the supplied features,
say so plainly.

Use the trading notes below as authoritative context for how the user
thinks about setups, what they consider valid signals, and their
personal rules. When notes and standard ICT definitions disagree, prefer
the notes.

--- NOTES START ---
{notes}
--- NOTES END ---
```

**User message (`build_user_message`):**

```
Symbol: {symbol}   Timeframe: {timeframe}   Current price: {current_price}

Detected features (last 200 candles):
{json.dumps(features, indent=2, default=str)}

Question: {query}
```

Both functions return plain strings.

**Tradeoff comment to include in `prompts.py`:** alternative is
function-calling / structured-output APIs. We're choosing flat text
because (a) Gemini 2.5 Flash handles compact JSON-in-prompt fine,
(b) it keeps debugging trivially copy-pasteable, (c) we're not
asking the model to produce structured output — only natural language.

## Step-by-step prompt

Working dir `/Users/ellometa/code/Experimenting/HAL`. Phases 1–3 are
complete. The OHLC fetcher and ICT detector exist and have passing
tests.

1. Implement `backend/prompts.py` with the two functions above. If
   `notes.md` doesn't exist, return an empty notes section with a
   `<no notes ingested yet>` placeholder.
2. Implement `backend/main.py`:
   - Pydantic model `AnalyzeRequest`: `symbol: str, timeframe: str, query: str`.
   - Lifespan: read `backend/notes.md` once at startup into a
     module-level string. (Reloads only on server restart — fine for v0.)
   - Reuse a single `genai.Client` across requests; create it in the
     lifespan startup.
   - `/analyze`:
       1. `df = await fetch_ohlc(req.symbol, req.timeframe)`
       2. `features = detect_all(df)`
       3. `current_price = float(df['close'].iloc[-1])`
       4. system_prompt = build_system_prompt(notes)
       5. user_msg = build_user_message(...)
       6. Call `client.models.generate_content(model="gemini-2.5-flash", contents=user_msg, config={"system_instruction": system_prompt})`
          (verify exact kwarg name — `google-genai`'s API surface is
          `types.GenerateContentConfig(system_instruction=...)` in the
          new SDK. Look it up if uncertain; do not guess.)
       7. Return JSON dict.
   - Wrap the whole thing in try/except. Map known errors to clean
     400s: invalid symbol → 400, unknown timeframe → 400. Unexpected
     → 500 with the exception message in the body (it's localhost only
     — leaking the trace is fine for v0 debugging).
   - Add CORS via `fastapi.middleware.cors.CORSMiddleware` allowing
     origins `chrome-extension://*` and `http://localhost:*`. Allow
     POST and OPTIONS, allow `Content-Type` header.
3. Add a `/health` GET returning `{"ok": True}`.
4. Run: `uv run uvicorn backend.main:app --reload --port 8000`
5. Test with curl:
   ```
   curl -s http://localhost:8000/health
   curl -s -X POST http://localhost:8000/analyze \
        -H 'Content-Type: application/json' \
        -d '{"symbol":"BTCUSDT","timeframe":"1h","query":"What is the most recent unmitigated bullish FVG, if any?"}' \
        | jq
   ```
6. STOP. Show curl outputs.

## Acceptance criteria

- `/health` returns 200 `{"ok": true}`.
- `/analyze` curl above returns 200 within ~5–10s, `answer` is a
  non-empty string, `features.fvgs` and `features.order_blocks` are
  present, `current_price` is a float close to the live BTC price.
- An invalid symbol (e.g. `"ZZZZUSDT"`) returns 400 with a readable
  message, not a 500 stack trace.
- An unknown timeframe (e.g. `"3h"`) returns 400.
- Two consecutive `/analyze` calls work — the client isn't
  single-use.

## Open questions / decisions

- **Q1.** Should we expose the prompt in the response for debugging?
  e.g. add `?debug=1` query param. Default: yes, gated behind a query
  param. Confirm.
- **Q2.** Reload notes on every request or only at startup? Default:
  startup — notes change infrequently and a manual server restart is
  cheap.
- **Q3.** Confirm `gemini-2.5-flash` is the right model id for this
  phase. (It is per the user's spec.) If Flash output is too thin
  later, consider 2.5 Pro — but this is a polish question.

## Out of scope

- No streaming. Phase 7.
- No Notion integration here — `notes.md` is read as-is. Phase 5
  populates it.
- No extension yet. Phase 6.
- No auth. localhost-only assumption holds.
- No request logging beyond uvicorn's defaults.
- Do not add request batching, queuing, or background tasks.
