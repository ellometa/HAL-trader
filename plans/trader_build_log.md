# Autonomous paper trader — build log

A running summary of what has been built on `feat/autonomous-paper-trader`,
why, and the honest state of the results. The canonical design doc is
`plans/10_paper_trader.md`; this file is the narrative of *what was done*.

> **Status:** paper-only. No live broker exists in code. 57 trader tests pass
> (90 across the whole repo).

---

## The shape of the thing

Fork B from `plans/ideation/07_autonomous_trading.md`: the model emits a
**structured `TradePlan`**, never touches the broker, and a **deterministic
validator** decides whether that plan is allowed and what size it gets. The
edge — if any — lives in the geometry layer the model can't talk its way
around, not in the prose it writes.

```
backend/trader/
  plan.py        TradePlan — the only thing the model may emit
  confluence.py  deterministic setup scorer — the edge the model can't inflate
  risk.py        validate_plan() invariants + RiskState halts (the safety surface)
  broker.py      PaperBroker — fills, slippage, fees, stop/TP resolution, stop management
  brain.py       features -> TradePlan: Gemini structured output, or a rule baseline
  engine.py      one decision: detect -> exits -> manage -> halt -> plan -> validate -> fill -> journal
  backtest.py    replay the live policy over history via the SAME engine.step
  evaluate.py    walk-forward: folds + pooled stats + buy&hold benchmark + calibration
  metrics.py     shared P&L math, so live-replay and backtest report identical numbers
  journal.py     append-only JSONL decision log
  service.py     the long-running loop + kill switch + lifecycle
  api.py         /trader/* endpoints + operator dashboard
scripts/
  backtest.py        run the live policy against fetched history (rule or llm)
  evaluate.py        walk-forward across symbols with A/B override knobs
  replay_journal.py  weekly-review tool: net P&L after costs, by confidence, by direction
config/trader.toml   every risk cap and loop knob, in one auditable file
```

---

## What was built, in order

| Commit | What | Why |
|--------|------|-----|
| `5bf58e2` | Autonomous paper trader (Fork B) | The base: plan → validate → paper-fill → journal, with halts + kill switch |
| `b58a2e8` | Backtest harness | Replay the live policy over history via the *same* `engine.step` — no second code path |
| `654e827` | Confluence scoring engine | The deterministic edge: score a setup from detector facts (zone + structure + FVG + sweep + freshness), 0..1 |
| `6a6e9a6` | Gate the rule policy on confluence; drop the flip-close bleeder | The structure-flip "manual" close was the single largest loss bucket; removed it |
| `7f00ccd` | No-chase entry guard | Enter on the *return* to a zone, not the run away from it — keeps stops tight and targets reachable |
| `e52dceb` | Active stop management | Breakeven (cost-covered) at +1R, trailing at +2R; stops only ever tighten; no look-ahead |
| `42300ba` | Deterministic per-bar `plan_id` | Wall-clock id collided across same-second backtest decisions, corrupting the confidence↔exit join |
| `c6dd51e` | Walk-forward evaluation harness | Judge a policy on a *distribution* of folds + a pooled result, not one lucky window |
| `61fffe3` | Benchmark + out-of-sample calibration | Measure each fold against buy-and-hold; pool stated-confidence across folds (it was being dropped) |
| `f4c466c` | Cost-aware reward:risk floor | Hold `min_rr` *net of round-trip fees* — reject 2:1 plans whose target can't clear its own cost |
| `2806906` | Wire the confluence floor into the validator | Closed a safety gap: `score_for_refs` was tested but never connected, so the gate bound only the rule policy — now it binds the LLM too |
| `8f4788f` | Feed the LLM the confluence read + cost reality | The model saw raw JSON but not the scorer's read or the true cost/floor rules; now it does |

---

## Evaluating without lying to yourself

A single backtest number is a trap. The keeper test is **walk-forward**:

- history is cut into contiguous, non-overlapping folds, each backtested
  independently (fresh equity, own warmup — no leakage across folds);
- two numbers come out — the **distribution** (how many folds green, the
  median/spread) and the **pooled** result (every trade in one bag);
- every fold is measured against **buy-and-hold over the same bars** — a
  policy that nets +1% while the market ran +10% destroyed value;
- stated confidence is pooled into a **calibration** table — if "high"
  confidence doesn't out-win "low" out of sample, the signal is noise.

A change is only worth keeping if it moves **both** — more folds green *and*
better pooled expectancy. Improving the pooled number while most folds stay
red is overfitting.

```bash
python -m scripts.evaluate BTCUSDT ETHUSDT --timeframe 1h --candles 1000 --folds 5
python -m scripts.evaluate BTCUSDT --candles 1000 --min-confluence 0.6   # A/B a knob
```

---

## The honest result

On BTC+ETH walk-forward, the **deterministic rule baseline has no
demonstrated edge** — it is a sanity floor, not a money-maker:

- **0/10 folds profitable** before the cost-aware floor, **2/10 after**.
- It beats buy-and-hold in roughly half the folds — i.e. it loses *less* than
  holding in a falling market. Defensive, not winning.
- Calibration is **inconsistent across symbols** (correctly ordered on BTC,
  inverted on ETH). A confidence signal that flips sign by symbol is not edge.

What measurably *helped*, each principle rather than a fitted knob:

- **cost-aware R:R floor** — on 1h this cut trades **105 → 20** and **halved
  the pooled loss (−428 → −189)** by rejecting trades whose targets couldn't
  clear their own fees. It removes loss; it does not invent a number.
- **no-chase entry guard** — fewer timed-out chases, tighter stops.
- **higher timeframe** — 1h roughly triples the rule policy's profit factor vs
  5m, because 5m is turnover- and noise-dominated.

None of this makes the rule policy profitable. That is the honest result, and
it is the *reason* the LLM path exists: the deterministic layer is the safe,
un-foolable substrate; any discretionary edge has to be earned on top of it
and proven the same walk-forward way — never on a single lucky window.

---

## Safety posture (unchanged, by design)

- **Paper is the only broker in code.** No `live = true`. Going live is a new
  module with a diff and a review, not a flipped flag.
- **The loop does not auto-start.** A restart never silently resumes trading.
- **Halts are asymmetric.** Daily-loss cap clears next day; equity floor and
  kill switch require a manual re-arm.
- **No look-ahead.** Detection on closed bars only; a fresh position is never
  exit-checked against a bar that closed at/before its entry.
- **The validator is the safety surface.** The model can only ever make a
  trade *less* likely: it can't cite a phantom setup, invert R:R, beat the
  cost-adjusted floor, clear too little confluence, oversize, or trade through
  a halt. If the model and validator disagree, the validator wins, silently.

---

## Open question — pending a backtest decision

The one thing not yet answered: does the now-well-informed **Gemini** brain
add edge over the deterministic floor? Testing it means a live LLM backtest
(spends API quota, non-deterministic), or genuine forward paper-testing (the
only truly out-of-sample arbiter). Deferred until you say how you want to run
it.
