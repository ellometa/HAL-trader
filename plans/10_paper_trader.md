# Phase 10 — autonomous paper trader (Fork B, built)

> Status: **built, paper-only.** This is the implementation of
> `plans/ideation/07_autonomous_trading.md` Fork B — "structured trade plan
> + dumb executor." It graduated from ideation to code because the design
> was defensible and the whole thing stays simulated. No live broker exists.

HAL is still a copilot on `/analyze`. This phase adds a *second, separate*
brain that wakes on its own, decides, and trades a **simulated** account.
The two share detectors, OHLC fetch, and notes; they share nothing else.

## What was built

```
backend/trader/
  plan.py        TradePlan — the only thing the model may emit
  risk.py        validate_plan() invariants + RiskState halts (the safety surface)
  broker.py      PaperBroker — simulated fills, slippage, fees, stop/TP resolution
  journal.py     append-only JSONL decision log
  brain.py       features -> TradePlan: Gemini structured output, or a rule baseline
  engine.py      one decision: detect(closed bars) -> exits -> halt -> plan -> validate -> fill -> journal
  backtest.py    replay the policy over history via the SAME engine.step the live loop uses
  metrics.py     shared P&L math, so live-replay and backtest report identical numbers
  service.py     the long-running loop + kill switch + lifecycle
  api.py         /trader/* endpoints + a self-contained operator dashboard
config/trader.toml   every risk cap and loop knob, in one auditable file
scripts/replay_journal.py   weekly-review tool: net P&L after costs, by confidence, by direction
scripts/backtest.py         run the live policy against fetched history (rule or llm)
tests/trader/        33 tests; every validator rejection path + every halt + backtest no-look-ahead
```

## The one rule that matters

The LLM never touches the broker. It emits a `TradePlan`; a deterministic
validator decides whether that plan is allowed and what size it actually
gets. The model can only ever make things *not* happen:

- it cannot cite a setup the detectors didn't find (`detector_refs` are
  checked against the real feature catalog — phantom refs are rejected);
- it cannot invert risk/reward (geometry + a min-R:R floor are enforced);
- it cannot oversize (size is clamped to both a notional cap and a
  per-trade risk cap, whichever is smaller);
- it cannot trade through a halt (daily-loss cap, trailing equity floor,
  or the manual kill switch).

If the validator and the model disagree, the validator wins, silently.

## How to run it

```bash
# 1. start the backend (trader is mounted but NOT auto-started)
uvicorn backend.main:app --reload

# 2. open the operator dashboard
open http://localhost:8000/trader/dashboard

# 3. drive it
curl -X POST localhost:8000/trader/decide?symbol=BTCUSDT   # one manual cycle
curl -X POST localhost:8000/trader/start                   # begin the loop
curl -X POST localhost:8000/trader/kill                    # flatten + halt
curl      localhost:8000/trader/status
curl      localhost:8000/trader/journal?n=20

# 4. weekly review
python -m scripts.replay_journal
```

Policy is chosen in `config/trader.toml`: `policy = "rule"` runs the
deterministic baseline with no API at all (doc 07's Phase A); `policy =
"llm"` has Gemini emit the plan (Phase B). If no model client is wired, the
engine falls back to the rule policy rather than doing anything risky.

## Safety decisions, on purpose

- **Paper is the only broker in code.** There is no `live = true`. Going
  live would mean writing a new broker module — a code change with a diff
  and a review, not a flipped flag. This is the "two-mode operation" control
  from doc 07, enforced by absence.
- **The loop does not auto-start.** A process restart never silently resumes
  trading; someone has to POST `/trader/start`.
- **Halts are asymmetric.** The daily-loss cap clears at the next day; the
  equity floor and the kill switch require a manual `/trader/rearm`. A bad
  day shouldn't quietly become a bad week.
- **No look-ahead.** Detection runs on closed bars only (`df.iloc[:-1]`); a
  freshly opened position is never exit-checked against a bar that closed at
  or before its entry.
- **Everything is journaled** — every decision, accepted or rejected, with
  the exact features and prompt that produced it. Any trade is traceable to
  the geometry and wording that caused it.

## Model note

Built on Gemini 2.5 Flash via structured output (`response_schema=TradePlan`)
because that's the key HAL already has, and structured output gives the
validator a typed object to reason about regardless of vendor. The single
model call is isolated in `brain._generate_plan_json`; swapping in another vendor
(doc 07's recommendation for the execution path) is a one-function
change, not a rewrite.

## What is deliberately NOT here

- No live broker, no real money, no credentials beyond the existing Gemini
  key. (Prohibited by design until the staged path says otherwise.)
- No extension panel yet — the dashboard covers operation; the TradingView
  panel from doc 07 would consume the same `/trader/*` endpoints.

## Graduation criteria (unchanged from doc 07)

- **A → B:** 100+ simulated trades execute cleanly; journal replays cleanly.
- **B → C:** 30+ days paper with zero validator-breach incidents and
  positive P&L *net of simulated fees + slippage* (the only metric that
  matters).
- **C onward:** backtest the live policy, then tiny live ("dinner money"),
  then scale only when you actually trust it. Not before.

The honest meta-note from doc 07 still stands: the edge here isn't the
model, it's the deterministic geometry layer the model can't lie about.
That discipline is the thing to protect at every step.
