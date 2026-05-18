# Data and state — where things live, how they flow

## Current shape

- **OHLC:** fetched on every `/analyze`. Binance for crypto regex,
  yfinance otherwise. No cache. ~150–400ms per call.
- **Notes:** `backend/notes.md`, loaded once into `_state.notes` at
  uvicorn startup. Re-ingestion script overwrites the file. Restart to
  reload (phase 8 P5 fixes this).
- **Chat history:** `chrome.storage.local`, keyed per `/chart/<slug>`,
  capped at 50 messages, no server-side knowledge.
- **No journal yet.** Every analysis is forgotten by both sides the
  moment it's rendered.
- **No metrics.** No request log, no token-cost log, no error rate.

## The big tension

For v0, statelessness on the server is a feature: no DB, no migrations,
no concurrency questions. Phase 9 (journaling) requires server state.
The architecture should be ready *before* phase 9 starts.

---

## Alt 1 — Switch state location: stop using `chrome.storage.local`

Move chat history to the server. Single source of truth.

**Pros**
- Survives extension reinstall, multi-device, browser data clear.
- Journaling builds on the same table — no migration "merge chrome storage
  into server" later.
- Server can index it. "Show me yesterday's BTC calls" becomes possible.

**Cons**
- Loses offline-survival of recent chats (panel goes dumb when backend down).
- Requires the auth-shaped question early ("which user am I?"). For a
  single-user local install, a static `user_id = "ellometa"` is fine.
- Synchronization gets non-trivial if you ever open multiple tabs.

**Verdict.** Worth doing *before* phase 9, not after. The "history is
in chrome.storage" decision was right for phase 6 (ship something) and
wrong for phase 9 onwards. Estimated rework: ~half a day.

---

## Alt 2 — A real database

Today: zero. Soon (journaling): SQLite is the obvious answer.

| Option | Right for HAL when… |
|---|---|
| SQLite | Always for v1. Single file, embedded in the backend process, FTS5 for keyword search over journal entries. |
| Postgres | Only if multi-user, or you want it hosted for "HAL anywhere." Massive overkill for a personal tool. |
| DuckDB | Tempting because of analytic queries on journal data. But journaling-shaped workloads (insert-heavy, small tables) are SQLite's home. |

**Schema first draft** (file in `backend/db/schema.sql`):

```sql
CREATE TABLE analyses (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,           -- unix ms
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  question TEXT NOT NULL,
  features_json TEXT NOT NULL,
  answer TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,     -- so prompt iteration can be tracked
  model TEXT NOT NULL,
  cost_usd REAL,                 -- optional, if we ever track it
  outcome TEXT,                  -- filled later: "win"/"loss"/"flat"/null
  outcome_notes TEXT
);
CREATE INDEX idx_analyses_symbol_ts ON analyses(symbol, ts DESC);
```

Pre-decided choices to spare bikeshedding later:
- Times as unix integers, not ISO strings. Indexable, no TZ ambiguity.
- `prompt_hash` over the assembled prompt — lets you tag "everything
  after I changed the system prompt" cleanly.
- `outcome` is optional, filled in via a small panel form after the
  trade resolves. Required for the "is HAL calibrated?" question.

---

## Alt 3 — Replace yfinance

yfinance is the single biggest source of operational flakiness in HAL.
Symptoms: random empty frames, schema changes between pip releases,
no SLA, rate-limited unpredictably.

Options:
1. **Twelve Data / Alpha Vantage** — free tier (~25 calls/day TD, 5/min
   AV). Stable schemas. Real keys needed. Probably worth the friction.
2. **Polygon.io** — paid but cheap ($30/mo personal). Excellent equity
   coverage, weak FX. Probably wrong for HAL.
3. **TradingView's own widget API via the extension** — read price
   data straight out of the chart we're already looking at. No HTTP at
   all. **Biggest payoff, biggest risk** (TV's DOM is unstable).
4. **Cache yfinance more aggressively** — accept flakiness, retry, use
   stale-while-revalidate. Smallest change.

**Verdict.** Combine #1 for forex (where yfinance is worst) with #4 for
equities. #3 is a phase-15+ idea, not v1.

---

## Alt 4 — Cache OHLC

Today every `/analyze` refetches even if you fired the same query twice
in 5 seconds. In-memory LRU keyed by `(symbol, timeframe, current_minute)`
gets you to one fetch per minute per chart, costs <1MB RAM, takes ~20
lines. Listed as phase 8 P7; mentioned here so it's not forgotten in
the larger picture.

**Stronger version:** cache *features* too, not just OHLC. Detection is
deterministic over OHLC, so the same OHLC frame gives the same features.
Saves another few ms.

---

## Alt 5 — Notes: from one file to chunked + retrieval

Current: full overwrite, full inject every call.

This is fine until notes.md exceeds ~200k chars (Flash's effective
context ceiling for the *whole* prompt including features payload).
Right now it's 30k. Plenty of headroom for v1.

**When notes grow:**

1. **Chunk by section heading.** Pre-compute on ingest. Each chunk gets
   a topic tag derived from its parent heading chain.
2. **Per-query retrieval.** Either (a) keyword-based, dirt cheap,
   prefilter by section, OR (b) embeddings via Gemini's embedding-001
   model, ~free at this scale.
3. **Hybrid.** Always include the table of contents + retrieved
   chunks. Model never loses the map even when it gets a small subset.

**Don't do this prematurely.** The plan in `new_plans.md` explicitly
parks RAG. Re-read once notes.md is >150k. Today: 30k. Don't bother.

---

## Alt 6 — Observability

There's none. For a personal tool that's fine until it isn't.

Minimum viable observability (~1 day):
- Every `/analyze` logs to SQLite (`analyses` table) regardless of
  whether journaling phase is built yet. Logging is independent of
  outcome tracking.
- A tiny `/stats` endpoint returning rolling counts, latency p50/p95,
  error rate.
- A console-only `print` of token counts per call (gemini's SDK returns
  this on the final chunk).

This makes the journaling phase 80% built already — you'd just be
adding the `outcome` field UI on top.

---

## Recommendation

Order, before phase 9 starts:
1. SQLite + `analyses` table, logging every call. Two days max.
2. Move chat history server-side, keyed by `analysis_id` chain. One day.
3. OHLC cache (phase 8 P7 — separate, already planned).

Park: replacing yfinance, RAG over notes, Postgres-anything.
