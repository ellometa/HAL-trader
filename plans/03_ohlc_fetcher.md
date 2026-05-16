# Phase 3 — OHLC Fetcher

## Goal

A single function `fetch_ohlc(symbol, timeframe, limit=200) -> pd.DataFrame`
that returns the last N candles for a symbol on a given timeframe.
Routes crypto symbols to Binance's public REST API; everything else
goes to yfinance. Pure I/O wrapper — no ICT logic.

## Context (what exists going in)

Phase 1 + 2 have produced:

- `backend/config.py`             (env loading)
- `backend/ict/*.py`              (detectors operating on DataFrames)
- `requirements.txt`              (has `httpx`, `yfinance`, `pandas`)

Detectors expect a DataFrame with columns: `timestamp` (UTC datetime),
`open`, `high`, `low`, `close`, `volume` — all floats except
`timestamp`. Row order: oldest → newest. This fetcher MUST produce
that exact shape.

## Deliverables

- `backend/ohlc.py`               — public `fetch_ohlc(symbol, timeframe, limit=200)`
                                    plus private `_fetch_binance(...)`,
                                    `_fetch_yfinance(...)`,
                                    `_is_crypto(symbol)` router
- `tests/test_ohlc.py`            — light tests that mock the network
                                    (e.g. `respx` for httpx) and verify
                                    schema. No live network calls in
                                    tests.

## Routing rule

`_is_crypto(symbol)` returns True if symbol matches `^[A-Z]{2,10}USDT?$`
or `^[A-Z]{2,10}USDC$` or `^[A-Z]{2,10}BTC$` (covers BTCUSDT, ETHUSDT,
SOLUSDC, etc.). Otherwise yfinance.

Document the assumption in a comment: "TradingView uses formats like
`BINANCE:BTCUSDT` or `NASDAQ:AAPL`; the extension will strip the
exchange prefix before sending, so we receive bare symbols here."

## Timeframe mapping

The backend exposes a normalized set: `1m, 5m, 15m, 1h, 4h, 1D, 1W`.
Map per-source:

- Binance: `1m, 5m, 15m, 1h, 4h, 1d, 1w` (lowercase d/w)
- yfinance: `interval="1m"|"5m"|"15m"|"1h"|"4h"|"1d"|"1wk"`
  - Note: yfinance doesn't support 4h directly. Fetch 1h and resample
    in pandas. Add a comment flagging this.
  - Note: yfinance limits intraday history (1m only ~7 days). Document
    the limit but don't try to fix it; for v0, intraday on equities is
    best-effort.

Add a `class TimeframeError(ValueError)` raised for unknown
timeframes.

## Binance API specifics

Endpoint: `https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=200`.

Response: list of arrays. Indices we care about:
- 0: open time (ms)
- 1: open
- 2: high
- 3: low
- 4: close
- 5: volume
- 6: close time (ms) — use this as the canonical `timestamp` so the
  candle's timestamp marks when it *closed*, not when it opened.
  Both conventions exist; closed-time is less ambiguous when joining
  detector output back to user-visible "what just happened" prompts.

Use `httpx.AsyncClient` with a 10s timeout. The function is `async`.

## Step-by-step prompt

Working dir `/Users/ellometa/code/Experimenting/HAL`. Phases 1 and 2
are complete. Read `backend/ict/detector.py` first to confirm the
expected DataFrame schema (`timestamp, open, high, low, close,
volume`).

1. Implement `backend/ohlc.py`:
   - `async def fetch_ohlc(symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame`
   - Route via `_is_crypto`.
   - Binance path: build URL, `httpx.AsyncClient` GET, parse to
     DataFrame, cast types, use close-time as `timestamp`.
   - yfinance path: yfinance is sync. Wrap with
     `await asyncio.to_thread(yf.download, ...)`. yfinance returns
     a DataFrame with MultiIndex columns when `group_by='ticker'`;
     either disable that or flatten. Lowercase column names. For 4h:
     download 1h, then `df.resample('4H', on='timestamp').agg({...})`.
   - Validate output shape before return; raise `RuntimeError` with
     a clear message if a source returns no data (e.g. unknown
     symbol).
2. Write `tests/test_ohlc.py`:
   - `_is_crypto` cases: `BTCUSDT`, `ETHBTC`, `AAPL`, `EURUSD` (the
     last is forex — should currently route to yfinance; flag this
     in a comment as something to revisit).
   - Mock the Binance response with `respx` (add to `requirements.txt`
     under uv's dev-deps group via `uv add --dev respx`).
     Test that the DataFrame has the right columns, dtypes, and
     ordering.
   - Mock yfinance with monkeypatch — patch
     `backend.ohlc._fetch_yfinance` and assert it's called for a
     non-crypto symbol.
3. Manual smoke (do this AND show output to user):
   ```
   uv run python -c "import asyncio; from backend.ohlc import fetch_ohlc; \
              df = asyncio.run(fetch_ohlc('BTCUSDT', '1h', 50)); \
              print(df.tail()); print(df.dtypes)"
   ```
4. Manual smoke with detector:
   ```
   uv run python -c "import asyncio; from backend.ohlc import fetch_ohlc; \
              from backend.ict.detector import detect_all; \
              df = asyncio.run(fetch_ohlc('BTCUSDT', '1h', 200)); \
              import json; print(json.dumps(detect_all(df), default=str)[:500])"
   ```
   Confirm at least one FVG or OB is found in 200 candles of live BTC
   data (it would be very surprising not to).
5. STOP. Show test results + both smoke outputs.

## Acceptance criteria

- `uv run pytest tests/ -v` → all tests green, no network calls.
- Manual `uv run python -c "..."` smoke against Binance returns a DataFrame
  with 50 rows, correct dtypes (`float64` for OHLCV, datetime for
  timestamp), and oldest-first ordering.
- `detect_all(df)` on real BTC 1h data returns a non-empty dict.
- The fetcher is async and uses `httpx.AsyncClient` (not `requests`).

## Open questions / decisions

- **Q1.** What does forex routing look like? `EURUSD` → yfinance
  supports `EURUSD=X`. Should the fetcher append `=X` automatically
  for forex pairs? Default: not in v0 — document the limitation,
  return the raw error.
- **Q2.** Cache OHLC responses for N seconds? Useful while testing,
  premature otherwise. Default: no cache in phase 3; add in phase 8
  polish if needed.
- **Q3.** What does the extension actually send for `symbol`? See
  phase 6 — for now assume bare symbol with no exchange prefix.

## Out of scope

- No retry logic, no rate limiting handling beyond basic timeouts.
- No support for futures / options / multi-leg instruments.
- No caching.
- No alternative data sources (Polygon, Alpaca, etc.).
- Do not import anything from `backend.ict.*` here — this module
  has no dependency on detector logic, and keeping it that way means
  it can be tested standalone.
