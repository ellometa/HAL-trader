# Brain topology — how the LLM interacts with the world

## What HAL does today (v0)

One round-trip per user query:

1. Backend fetches 200 candles.
2. Runs all detectors deterministically.
3. Builds a single string: system prompt (with full `notes.md`) + user
   message (symbol, TF, price, full features JSON, question).
4. Streams Gemini's reply.

**Single-shot stuffing.** Cheap, debuggable, no orchestration. Works
beautifully *when the question matches the prepared payload*. Fails or
hallucinates when the question needs something outside the payload (a
different timeframe, a different symbol's bias, the user's own past
trades, today's news).

---

## Alternative A — Tool-calling agent loop

Gemini 2.5 Flash supports function calling. Architecture flips: the
model decides which deterministic functions to call.

Tools exposed:
- `fetch_ohlc(symbol, timeframe, n=200)` — current OHLC fetcher
- `detect_features(symbol, timeframe)` — current detector pipeline
- `query_notes(topic_or_keyword)` — small in-memory keyword search over notes.md
  (or vector search once notes exceed ~200k chars)
- `read_journal(filter)` — once phase 9 lands
- `current_price(symbol)` — cheap, separate from full OHLC

Loop: model receives the question, emits a tool call, server runs it,
result returns, model continues. Stops when model emits final text.

### Why this matters

Three concrete capabilities current HAL can't do well:

1. **Multi-TF without ballooning the prompt.** Model asks for 4h *and*
   1h features only when the question is structural. Otherwise it
   skips the extra fetch.
2. **Cross-symbol questions.** "Is BTC dominance rising?" — model
   fetches BTC.D itself.
3. **Note triage.** Instead of stuffing 30k chars of notes every call,
   model retrieves only the chunks it needs. Reduces noise *and* cost
   at scale.

### Costs

- Latency: 2–4× current. Each round-trip is a full Gemini call. Streaming
  partially hides it but the time-to-first-token jumps.
- Complexity: backend now needs a real loop with tool-result wiring,
  retry, max-iterations, malformed-call handling.
- Determinism erosion: same question can take different paths. Harder
  to debug. (Mitigation: log every tool trace.)
- Streaming UX gets harder — tokens flow only during the final
  generation, with awkward gaps during tool execution. Need a
  status-event channel ("running detect_features…") to keep the panel
  alive.

### When to switch

Switch when you have ≥2 of these:
- You're hitting a token-budget ceiling on the stuffed prompt.
- You want multi-TF / multi-symbol questions natively.
- The journal exists and you want HAL to query its own past calls.

If none of those, single-shot stays the right call.

---

## Alternative B — Structured-output mode for "plan this trade"

Hybrid: keep single-shot for chat, add a *second* endpoint (`/plan`)
that uses Gemini's structured-output mode and returns a fixed JSON
schema:

```json
{
  "bias": "bearish",
  "entry_zone": {"low": 80407.6, "high": 80776.33},
  "stop": 81000,
  "targets": [78650, 77800],
  "invalidation": "close above 81000",
  "rr": 2.3,
  "notes": "Confluence: unmitigated bearish OB + closest bearish FVG. ..."
}
```

Renders as a tidy card in the panel, not free text. Pairs naturally
with phase 9 — the journal logs structured plans against outcomes,
which is the only way to back-test the model's calibration.

This is **additive**, not a replacement. Low-risk to land.

---

## Alternative C — Two-pass: cheap router + targeted full pass

For every query, first do a tiny cheap pass ("router") that decides
which features and notes are relevant. Then the main pass receives
*only* that subset.

- Router model: Gemini Flash with a 200-token prompt, output JSON like
  `{"needed_features": ["fvg"], "needed_note_topics": ["module_6"]}`.
- Main model: same as today but prompt is half the size on average.

Cheaper at scale, slightly slower per call, decoupled from agent
loops. A reasonable middle ground if Alt A feels too heavy.

---

## Alternative D — Pure prompt-stuffing forever, optimize the stuffing

Most boring, possibly correct. Specifically:

- Add a structured "context budget" — features always included; notes
  pruned to the chunks whose section headings match a TF-IDF score
  against the query.
- Move from prose system prompt to a more rigorous instruction format
  with explicit role examples.
- Switch model to Gemini 2.5 Pro for complex queries only (model-of-the-day
  router heuristic).

Stays single-shot, but gets sharper. ~1 week to ship.

---

## Recommendation

**Year-1 path:** D first (sharpen what works), B second (structured
plan endpoint, ties to journaling), A last and only if (1) journal
exists and is interesting, OR (2) cross-symbol/cross-TF becomes a
daily friction.

**Avoid C.** Two-pass adds machinery without unlocking new capability.
The cost of "send a bit more context every call" is already cheap on
Flash; saving 20% tokens isn't worth a whole new layer.

## Confidence

High that D should happen first. Medium that A is the right v2 shape —
the bet is that ICT analysis really does benefit from active retrieval
over notes, and that's only true if your notes keep growing.
