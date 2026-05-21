# Autonomous trading — can HAL trade itself?

> Theory, not commitment. This is the largest possible expansion of
> HAL's surface area and the one with the most asymmetric downside.
> Read with that in mind.

HAL today is a *copilot*. You bring the chart, you bring the question,
you place the order. The LLM never touches your account. This doc is
about what would change to make HAL a *trader* — a process that wakes
up on its own, decides, and (eventually) sends orders.

It would be a strict superset of current HAL: every detector, every
note, every prompt stays. What's added is **state, scheduling,
execution, and risk gating**. Most of the design effort is in the last
two; the first two are mechanical.

---

## The short answer

**Yes, it's buildable. No, you shouldn't start by trading real money.**

There is a defensible architecture: keep the deterministic detectors
as ground truth, let the model emit a *structured trade plan* (not raw
orders), and put a dumb but strict rule engine between the model and
the broker. Run it in paper mode for weeks. Graduate to live with a
position cap so small that a total blowup costs less than a dinner.

Most public Claude/LLM trading bot experiments that survive contact
with reality look like this. Most that blow up skipped the rule engine
or skipped the paper phase.

---

## What HAL has that's actually useful for this

This is the part the public experiments mostly *don't* have, and the
reason HAL is unusually well-positioned:

- **Deterministic geometry.** FVG, OB, BOS/CHoCH, liquidity sweeps are
  computed in Python, not hallucinated by the model. The model never
  invents a setup — it only narrates what the detectors found. For an
  autonomous bot this is huge: most LLM-trader failures trace back to
  the model fabricating structure that isn't there.
- **A trading notes substrate.** `notes.md` already encodes *your*
  rules. The autonomous variant inherits them — no second source of
  truth.
- **A streaming I/O loop and OHLC fetch + cache.** The plumbing for
  "wake up, look at the market, think" already exists at request
  granularity.
- **Pine mirrors for audit.** If the bot claims a BOS triggered an
  entry, you can look at the chart and see the exact same BOS the
  Python code saw. Most bots have no such trail.

The gap isn't analysis. It's *acting* and *being safe while acting*.

---

## What an autonomous HAL would need that today's HAL doesn't

| Component | Today | Needed |
|---|---|---|
| Trigger | User question via panel | Scheduler — wakes on bar close or interval |
| Account state | None | Cash, positions, open orders, unrealized P&L |
| Execution | None — user clicks | Broker adapter (paper → live), order lifecycle |
| Risk gating | Implicit (human eyeballs) | Hard caps, daily loss stop, drawdown halt, kill switch |
| Decision log | Per-request prose only | Append-only journal of every decision + inputs |
| Output shape | Free-text prose | Structured `TradePlan` validated by Pydantic |
| Memory | Per-session chat history | Position lifecycle memory (entry reason → exit reason) |
| Kill path | Close the panel | One-click halt + automatic halts on guardrail breach |

Roughly: HAL becomes a stateful long-running process with an external
side effect surface. That's a different operational class than what it
is today.

---

## Three architectural forks

### Fork A — LLM-as-trader (high autonomy, high risk)

Every N seconds (or every bar close) the model gets:

- Current OHLC features (the existing detector JSON)
- Current portfolio state
- Open orders
- Recent decision history

…and emits trade actions via tool calls (`open_long`, `close`,
`set_stop`, `do_nothing`). The model is the policy.

This is what most public Claude trading bots look like (SharkBot, the
various "I gave Claude $X" experiments). It's also where most failures
live: hallucinated setups, direction bias drift, reckless leverage
choices when the model is uncertain.

**Verdict:** seductive, brittle. Don't start here.

### Fork B — Structured trade plan + dumb executor (recommended)

The model never sees the broker. It sees features + notes + portfolio
and emits a single object:

```python
class TradePlan(BaseModel):
    action: Literal["open_long", "open_short", "wait", "close_existing"]
    rationale: str                  # the why, logged
    entry: float | None
    stop: float | None
    take_profit: float | None
    size_pct_equity: float | None   # bounded server-side
    validity_bars: int              # plan expires after N bars
    confidence: Literal["low", "medium", "high"]
    detector_refs: list[str]        # which detector IDs justify this
```

A separate `executor.py` checks invariants:

- size ≤ max_position_pct
- aggregate exposure ≤ max_portfolio_pct
- correlated-asset cap not exceeded
- daily realized loss < daily_loss_cap
- equity > equity_floor (else halt)
- `detector_refs` actually exist in current features (no phantom OBs)

If any invariant fails → reject the plan, log it, halt or wait. The
model's failure modes get caught by the *plan validator*, not by the
market.

**Verdict:** this is the architecture worth building. It preserves
HAL's "detectors are truth" stance and uses the LLM only where it
adds value (interpreting the configuration of detected events).

### Fork C — Rule engine with LLM advisor (lowest autonomy)

A deterministic state machine decides entries from detector events
directly (e.g. "long when bullish OB forms inside a discount, BOS up,
liquidity sweep within last 5 bars"). The LLM only gets called to
*veto* — given the rule's reason, is there a notes-based reason to
skip this trade?

**Verdict:** safest, smallest, but you're essentially writing a
traditional ICT bot and only using the LLM for taste. Not a bad
fallback if Fork B keeps tripping on plan-validation rejections.

---

## What the public experiments actually show

A few honest data points worth weighting before committing:

- **SharkBot, Christmas 2025 (Sonnet 4.5 vs Haiku 4.5, Binance
  Futures, ~45 hours live).** Sonnet hit +3.44% one session, -4.21%
  another; Haiku lost 21.6% over 23.8h. Two findings matter: Sonnet
  adapted direction (55/45 long/short), Haiku stayed 86% long across
  regime changes; Haiku used 20x leverage on 27% of trades, Sonnet
  capped near 10x. The model choice changed everything.
- **"14 sessions, 961 tool calls" bot.** Of 5 strategies tested across
  26,000 5-min candles, only 1 survived (EMA momentum, +0.32%, 120
  trades, 33% win rate, profit factor 1.08). The killer was trade
  frequency — high-frequency strategies died on fees + slippage.
- **The 60% / -$39 result.** A 60% win rate that still lost money
  because the R:R was inverted. Surface metrics are not safety. Net
  P&L after costs is the only metric that matters.
- **Polymarket headlines.** Eye-catching wins exist; ~92% of wallets
  on the platform are net unprofitable. Survivorship bias is the
  default lens for any LLM-trading content.

The pattern: bots that survive have **few signals, hard risk caps,
and structured decision output**. Bots that blow up have **frequent
signals, model-driven sizing, and free-text reasoning that nobody
audits**.

---

## Failure modes specific to LLM-driven trading

Beyond ordinary algo-trading failure modes, the LLM stack adds:

1. **Strategy hallucination.** Model invents a setup that wasn't in
   the detector output. Mitigation: validator checks
   `plan.detector_refs` exist in `features`. Reject otherwise.
2. **Direction bias drift.** Training-time priors leak through. The
   Haiku 86%-long stat is exactly this. Mitigation: log rolling
   long/short ratio, alert if it stays one-sided across regime shifts.
3. **Confidence inflation.** Model says "high confidence" on every
   plan. Mitigation: track P&L conditional on stated confidence; if
   "high" doesn't outperform "low" empirically, ignore the field.
4. **Inverted R:R hidden under good win rate.** Mitigation: validator
   rejects any plan where `(target - entry) / (entry - stop) < 1.5`.
5. **Correlated exposure stacking.** Ten "independent" longs on BTC,
   ETH, SOL is one big crypto-beta long. Mitigation: aggregate
   exposure cap by asset class, not per-symbol.
6. **Cost asymmetry.** A 1-minute Sonnet decision loop is roughly
   $20–60/day in API spend. Edge has to clear that before clearing
   spread + fees. Most don't.
7. **Stale-data action.** Cached OHLC is fine for advice; for an
   executor it's a risk. Cache TTL must be ≤ shortest timeframe traded.
8. **Prompt drift across sessions.** Quietly editing `notes.md`
   changes the policy mid-flight. Mitigation: snapshot the prompt
   into the decision log so every order traces to the exact text that
   produced it.

---

## Non-negotiable risk controls

If any of these aren't in place, don't connect a live broker. Period.

- **Per-trade size cap** (e.g. ≤ 2% equity at risk).
- **Daily realized-loss cap** — halt the bot for the rest of the day.
- **Trailing equity floor** — halt permanently if equity drops below
  X% of high-water mark; require manual re-arm.
- **Position concurrency cap** — N open positions max.
- **Asset-class exposure cap** — total long-crypto exposure ≤ Y%.
- **Manual kill switch** — endpoint + panel button. Cancels all open
  orders, flattens positions if configured, then halts.
- **Heartbeat watchdog** — if the decision loop hasn't ticked in N
  minutes, flatten and alert.
- **Append-only decision journal** — every plan (accepted *and*
  rejected) with the exact features + prompt that produced it.
- **Two-mode operation** — `paper` and `live` are different processes
  with different config, never a runtime flag, so a stray env var
  can't promote paper to live.

---

## Model choice: stay on Gemini, or switch to Claude?

HAL runs Gemini 2.5 Flash today. The public bot evidence is mostly
Claude. Is there a reason to switch?

**For prose-only advice (current HAL):** Gemini Flash is fine. Cheap,
fast, the output is consumed by a human who corrects anything wrong.

**For autonomous execution:** there's a real case for Claude Sonnet,
specifically because:

- Tool-use discipline is better-tested at Anthropic. Sonnet is less
  prone to emit malformed tool calls or invent fields.
- The SharkBot result is one of the only head-to-head datapoints we
  have, and Sonnet didn't blow the account up. Haiku did. The cheap-
  and-fast model is *not* the right pick when the output controls a
  position.
- Structured-output reliability (the `TradePlan` schema) matters more
  than raw throughput.

A clean approach: keep Gemini Flash on `/analyze` (prose path),
introduce a separate `/decide` endpoint that calls Claude Sonnet and
returns a validated `TradePlan`. Two models, two jobs, two cost
profiles. Decided per-call, not globally.

Worth noting: this is the first thing in HAL that would justify a
multi-model setup. Until now there's been no good reason.

---

## The minimal new code surface

If/when this ships, roughly:

```
backend/
  trader/
    plan.py              TradePlan schema
    executor.py          invariant checks + order routing
    journal.py           append-only decision log (sqlite)
    risk.py              caps, daily-loss tracking, kill switch
    schedulers/
      bar_close.py       wake on timeframe boundary
      interval.py        wake every N seconds
    brokers/
      paper.py           in-memory simulated fills (default)
      alpaca.py          equities/crypto via Alpaca
      binance_testnet.py crypto futures testnet first
  decide.py              new endpoint: features → TradePlan (Claude)
extension/
  trader_panel.js        separate UI: state, P&L, kill switch
  trader_panel.css
config/
  trader.toml            risk caps, broker creds, paper vs live
scripts/
  replay_journal.py      re-run decisions against historical data
tests/
  trader/                heavy — every invariant has a test
```

The `tests/` directory is not optional and not small. The risk
invariants are the safety surface; if they're not covered, the bot
isn't covered.

---

## A staged path

Each phase has an explicit graduation criterion. Don't skip steps —
the cost of doing so is asymmetric and unrecoverable.

**Phase A — Paper trader scaffolding.** `TradePlan` schema, paper
broker, decision journal, scheduler. No model yet — use a hardcoded
rule (e.g. "long every bullish OB confirmed by BOS") to prove
end-to-end pipes.
→ *Graduate when:* 100+ simulated trades execute cleanly; journal
replays produce identical results.

**Phase B — LLM plan generation (paper).** Add `/decide` endpoint.
Claude Sonnet emits `TradePlan` from current features + notes. All
invariants enforced. Still paper.
→ *Graduate when:* 30+ days of paper trading with no validator-
breach incidents; positive paper P&L net of *simulated* fees + slippage.

**Phase C — Backtest the live policy.** Replay the journal against
historical candles for the same period; compare simulated paper P&L
vs what the policy would have done historically. Catch look-ahead
bugs.
→ *Graduate when:* backtest and paper match to within reasonable
variance; no surprising look-ahead leaks.

**Phase D — Tiny live (the "dinner money" phase).** Connect a real
broker with a maximum account size you'd be okay losing entirely
(e.g. $200). All caps stay strict.
→ *Graduate when:* 60+ days live, no kill-switch events, P&L
trajectory plausibly matches paper (not necessarily profitable —
matching paper is the actual bar).

**Phase E — Scale carefully.** Increase account size in steps. Re-
read the journal weekly. Recompute conditional P&L by `confidence`,
by detector, by timeframe. Kill what doesn't work.
→ *Graduate when:* you actually trust it. Not before.

**Phase F — Ongoing.** The detectors evolve, notes evolve, the model
will evolve. Every change is a regime change. Treat it like one —
re-validate.

---

## Cost reality check

Order of magnitude, current Sonnet pricing, one decision per minute
across 3 symbols on a 24/7 crypto market:

- ~4,300 decisions/day per symbol → ~13,000 total
- ~3K input tokens + ~500 output per decision (features payload is
  small, but notes + system prompt aren't)
- Roughly $30–60/day in API spend depending on caching

Daily edge must clear that *before* clearing spread + fees + slippage
*before* counting as profit. For a $200 account this is obviously
absurd — that's by design, you're not optimizing for return at that
size, you're paying tuition. At what account size does the math
start to work? Roughly: if you expect 0.3% net daily edge (very
optimistic), you need ~$15-20K of float just to outrun API cost. That
number is worth staring at before starting.

---

## What we'd be giving up

The thing this folder keeps coming back to: HAL today *fits in one
head*. You can read the whole repo in an afternoon. An autonomous
variant changes that permanently.

- **Mental model.** "Chart copilot I ask things" → "process that
  trades while I sleep." Different psychological relationship.
- **Failure surface.** Currently the worst HAL bug is a wrong answer
  in a panel. Worst autonomous-HAL bug is a six-figure-relative loss.
- **Operational surface.** Localhost-only request/response becomes a
  long-running daemon with credentials, persistent state, and an
  outbound side-effect channel. That deserves its own threat model.
- **Joy-to-effort ratio.** Building it is fun. Operating it is not.
  Most days it'll just be a thing you nervously check.

None of this is a reason not to do it. It's a reason to do it
deliberately, with the rule engine in place from day one, and never
to skip the paper phase.

---

## Honest meta-note

The literature here is loud and survivorship-biased. The headline
results ("I gave Claude $100K and it beat the market") are
unfalsifiable single-runs on small samples in regimes that won't
repeat. The boring results — "60% win rate, still lost money because
R:R was inverted" — are the ones to internalize.

HAL's specific edge for this isn't the model. It's the discipline of
having a deterministic geometry layer that the model can't lie about.
If you build the autonomous variant, that discipline is the thing
worth protecting at every step. The day the model is allowed to
invent a setup, the project becomes a different and much worse
project.

Build Fork B. Stay in paper for a long time. Don't skip the journal.
