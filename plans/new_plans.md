# Future phases (post v0)

Theorized phases beyond the locked 1–8 plan. None of these have
standalone-prompt files yet — they're parked for after streaming +
polish land. Ordered by leverage, not by build difficulty.

---

## Phase 9 — Trade journaling

**The highest-leverage thing to build after v0.** Every HAL analysis
writes a row to a local SQLite log:

- timestamp, symbol, timeframe
- the features payload (FVGs, OBs, etc.)
- the assistant's answer
- optional outcome field (filled in by user after the trade resolves)

Unlocks queries against your own history: "show me every time HAL said
'bullish OB at discount' and I lost money", "what % of unmitigated FVGs
I traded actually held". Turns chat into a feedback loop.

~2 days. Schema design + a `/journal` panel tab. No new ML.

---

## Phase 10 — Multi-timeframe context

ICT is fundamentally top-down: daily bias → 4h structure → 1h entries.
Right now HAL sees one TF at a time, which is the wrong shape for the
methodology.

Concrete change: when the user asks a question on TF *X*, the backend
also fetches the two HTFs above *X*, runs detection on all three, and
stuffs all three feature blocks into the prompt. Token cost ~3× current,
still bounded. Quality jump for swing/positional questions.

---

## Phase 11 — Setup alerts

Backend gains a tiny scheduler that polls a watchlist on intervals
and pushes notifications when specific setups appear (e.g. "new
unmitigated bullish OB on EURUSD 4h", "BOS to upside on BTC 1h").

Reuses the existing detector. Notification surface: browser
notification API + badge on the HAL button. Touchy point: deciding
*which* conditions are "alert-worthy" is itself a prompt-engineering
problem — start with a fixed allow-list, let it grow.

---

## Phase 12 — Structured trade plan output

A "Plan this trade" button on the panel. Returns Gemini's response in
a fixed JSON schema rather than free text:

```
{ bias, entry_zone, stop, targets[], invalidation, rr, notes }
```

Renders as a tidy card. Uses Gemini's structured-output mode (different
prompt from the chat one). Pairs naturally with phase 9 — the journal
logs structured plans against outcomes.

---

## Phase 13 — More ICT primitives

Phase 8 covers structure (BOS/CHoCH) and liquidity sweeps. The longer
tail:

- breaker blocks
- inversion FVGs (IFVG)
- killzones (time-of-day filters: London, NY AM, NY PM)
- IPDA reference arrays
- Power-of-3 (AMD: accumulation, manipulation, distribution)

Each is a new file in `backend/ict/` + tests + possibly a Pine variant.
Add them on demand, not pre-emptively. Driver = "I keep manually asking
HAL about X" → build X.

---

## Phase 14 — Teach mode toggle

A header toggle: "Trade" vs "Teach".

- Trade mode (default, what you have now) → tactical analysis.
- Teach mode → same features, different system prompt. Explains *why*
  the detected feature matters in ICT theory, links back to specific
  Notion module ("this is the case Module 6 describes when…").

Useful while you're still working through the primer. Two prompts in
`backend/prompts.py`; one switch in the extension.

---

## Things deliberately NOT to build

- **Multi-symbol screener** ("rank my watchlist"). Flashy, doesn't
  deepen any single analysis.
- **Voice input.** Niche, latency-bad, keyboard is already at hand.
- **Local LLM routing.** Infrastructure cost for marginal savings on
  a personal tool.
- **Notes RAG / embeddings.** Locked out from v0 for a reason. Brute
  stuffing has worked so far; revisit only if notes exceed ~200k chars.
- **Backtesting features against historical price.** Sounds rigorous,
  but the answer space is too high-dimensional for short series to
  mean anything. The journaling phase is the right shape for this need.

---

## Recommended order after v0

1. Phase 7 (streaming) — already planned.
2. Phase 8 (polish) — already planned, includes the Pine FVG/OB
   highlighter as a visual rail.
3. **Phase 9 (journaling)** — the next real chapter. Without it, no
   way to know if HAL's analysis is calibrated to your trading.
4. Phases 10–14 in any order, driven by what you actually find missing
   while using v0+9.

Past phase 9 the bottleneck is your own usage data, not features.
