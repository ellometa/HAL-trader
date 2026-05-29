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
  confluence.py  deterministic setup scorer — the edge the model can't inflate
  risk.py        validate_plan() invariants + RiskState halts (the safety surface)
  broker.py      PaperBroker — fills, slippage, fees, stop/TP resolution, active stop management
  journal.py     append-only JSONL decision log
  brain.py       features -> TradePlan: Gemini structured output, or a confluence-gated rule baseline
  engine.py      one decision: detect(closed bars) -> exits -> manage stop -> halt -> plan -> validate -> fill -> journal
  backtest.py    replay the policy over history via the SAME engine.step the live loop uses
  evaluate.py    walk-forward: independent folds + pooled stats + buy&hold benchmark + calibration
  metrics.py     shared P&L math, so live-replay and backtest report identical numbers
  service.py     the long-running loop + kill switch + lifecycle
  api.py         /trader/* endpoints + a self-contained operator dashboard
config/trader.toml   every risk cap and loop knob, in one auditable file
scripts/replay_journal.py   weekly-review tool: net P&L after costs, by confidence, by direction
scripts/backtest.py         run the live policy against fetched history (rule or llm)
scripts/evaluate.py         walk-forward across symbols with A/B override knobs (the anti-overfit tool)
tests/trader/        51 tests; every validator rejection path + every halt + no-look-ahead + confluence + management + walk-forward
```

## The one rule that matters

The LLM never touches the broker. It emits a `TradePlan`; a deterministic
validator decides whether that plan is allowed and what size it actually
gets. The model can only ever make things *not* happen:

- it cannot cite a setup the detectors didn't find (`detector_refs` are
  checked against the real feature catalog — phantom refs are rejected);
- it cannot invert risk/reward (geometry + a min-R:R floor are enforced),
  and the floor is measured **net of round-trip costs** — a 2:1 plan whose
  target sits closer than the fee is rejected, not filled;
- it cannot oversize (size is clamped to both a notional cap and a
  per-trade risk cap, whichever is smaller);
- it cannot trade through a halt (daily-loss cap, trailing equity floor,
  or the manual kill switch).

If the validator and the model disagree, the validator wins, silently.

Underneath the validator sits a second deterministic layer the model also
can't fake: `confluence.py` scores a setup from detector facts alone (zone +
aligned structure + unfilled FVG + recent sweep + freshness), normalised to
0..1, with a no-chase guard that refuses entries far from the zone edge. The
rule baseline trades only when that score clears `min_confluence`; the score
also becomes the stated confidence, so calibration is checkable later. The
point is the same one doc 07 makes: the edge, if any, lives in geometry the
model can't talk its way around — not in the prose it writes.

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

# 5. judge a policy change honestly (walk-forward, never a single window)
python -m scripts.evaluate BTCUSDT ETHUSDT --timeframe 1h --candles 1000 --folds 5
python -m scripts.evaluate BTCUSDT --candles 1000 --min-confluence 0.6   # A/B a knob
```

## Evaluating without lying to yourself

A single backtest number is a trap: tune a knob until one slice looks good
and you've fit noise, not an edge (doc 07 names this explicitly). So the
keeper test is **walk-forward**, not one window:

- the history is cut into contiguous, non-overlapping folds, each backtested
  independently (fresh equity, its own warmup — no state or look-ahead leaks
  across folds);
- two complementary numbers come out: the **distribution** (how many folds
  were profitable, the median/spread of returns) and the **pooled** result
  (every trade in one bag, scored once);
- every fold is measured against **buy-and-hold over the same bars** — a
  policy that nets +1% while the market ran +10% destroyed value, and a bare
  return number hides that;
- stated confidence is pooled across folds into a **calibration** table — if
  "high" confidence doesn't out-win "low" out-of-sample, the confidence
  signal is noise (or worse, inverted) and can't be trusted.

A change is only worth keeping if it moves *both* — more folds green AND
better pooled expectancy. Anything that improves the pooled number while
most folds stay red is almost always overfitting.

## What the harness actually says (the honest part)

Run against BTC+ETH, the **deterministic rule baseline has no demonstrated
edge** — it is a sanity floor, not a money-maker. On 1h/1000 bars/5 folds it
was profitable in 0/10 folds before the cost-aware floor and 2/10 after; it
beats buy-and-hold in roughly half the folds (it loses *less* than holding
in a falling market — defensive, not winning). Calibration is inconsistent
across symbols: correctly ordered on BTC, inverted on ETH. A confidence
signal that flips sign by symbol is not an edge.

What measurably *helped*, and why each is principle rather than a fitted
knob:

- **no-chase entry guard** — enter on the return to a zone, not the run away
  from it, so stops stay tight and targets stay reachable;
- **cost-aware R:R floor** — refuse trades whose target can't clear their own
  round-trip fee. On 1h this cut trades 105→20 and halved the pooled loss
  (−428→−189). It removes loss; it does not invent a number;
- **higher timeframe** — 1h roughly triples the rule policy's profit factor
  vs 5m, because 5m is turnover- and noise-dominated.

None of this makes the rule policy profitable. That is the honest result,
and it is the *reason* the LLM path exists: the deterministic layer is the
safe, un-foolable substrate; the discretionary edge (if it is anywhere) has
to be earned on top of it and proven the same walk-forward way — never on a
single lucky window.

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
