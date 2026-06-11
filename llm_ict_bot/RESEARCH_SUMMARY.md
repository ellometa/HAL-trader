# Research Summary — LLM + ICT Hybrid Trading Bot (EUR/USD)

**Question.** Does a deterministic ICT/Smart-Money-Concepts strategy — optionally
with an LLM as the decision-maker — produce positive risk-adjusted returns on
EUR/USD intraday, out of sample and after costs?

**Answer.** No. Across three independent tests on a leakage-safe backtest, the
sweep-reversal ICT playbook had **negative expectancy**, an LLM decision layer
**added no edge**, and an 864-configuration parameter search that was profitable
in-sample **failed entirely out-of-sample**. The strongest single result: over a
24-month, 89-trade window the strategy returned **−15.4%** while simply holding
EUR/USD returned **+6.0%**.

This is a negative result, reported honestly. The value delivered is the
methodology and the infrastructure, not a profitable strategy.

---

## The strategy under test

Mechanical ICT confluence, evaluated at every New-York-session 15-minute close
while flat:

1. **Liquidity sweep** — price wicks through a recent 15m swing level and closes
   back inside (a stop-run).
2. **Structure shift** — a 15m BOS/CHoCH in the sweep's reversal direction,
   confirmed *after* the sweep.
3. **Premium/discount filter** — on the 1h dealing range, longs only from
   discount, shorts only from premium.

Entry next 1m open; stop beyond the sweep wick (±2 pips, min 5-pip stop); take
profit at 2R; 1% equity risk per trade; 1-pip round-trip spread.

The LLM variant receives the *same* detector events as point-in-time context and
makes the long/short/none call itself, constrained by the same validator and risk
engine — isolating exactly one variable: **mechanical rules vs. LLM judgment over
identical information.**

## Execution integrity (verified)

The −15% results are the strategy losing money, not a plumbing bug. Confirmed against
the trade logs:

- Every trade exits via stop-loss or take-profit, enforced intrabar on 1m data,
  **stop-loss-first** when a bar spans both (pessimistic).
- Position sizing is genuine 1%-of-equity: `units = risk_usd / |fill − SL|`, so
  wider stops take smaller size (dollar risk stays ~$1,000 on $100k).
- Costs are real: losses come in slightly worse than −1R and wins slightly under
  +2R — the spread drag, larger in R-terms on tighter stops (an 8-pip stop loses
  ~1.125R).
- Leakage controls: `close_time`/`confirmed_time` discipline, machine-checked by a
  prefix-consistency property test across all 8 detectors; next-bar fills only.

---

## Test 1 — LLM vs. rules, February 2026 (live decision layer)

Real LLM walk-forward, 719 consultations, 0 transport errors.

| strategy | model | trades | return | Sharpe |
|---|---|---|---|---|
| LLM | llama3.1:8b (local) | 1 | −1.09% | −3.31 |
| LLM | llama3.2:3b (local) | 0 | 0.00% | — |
| rule-only ICT | — | 4 | −1.31% | −1.43 |
| buy-and-hold | — | — | −0.33% | −0.86 |

The 8B proposed 10 entries but 9 failed the reward:risk validator and reverted to
"no trade"; its one surviving trade stopped out for −1R. The 3B abstained on all 578
decisions — a degenerate "never trade" policy, not skill. **The LLM did not beat the
mechanical rules; it added cost and latency without edge.** (One month is a thin
sample — hence Tests 2 and 3.)

## Test 2 — Parameter search, in-sample vs. out-of-sample

864 configurations over 8 knobs (sweep window, killzone timing, CHoCH/BOS
strictness, premium-discount timeframe, FVG/displacement confluence, RR target, min
stop). Tuned on **2021–2024**, gated for robustness (positive in ≥3 of 4 years, ≥40
trades, PF ≥ 1.15), then the survivors validated **once** on untouched **2025 →
2026-06**.

- **In-sample:** 16 robust survivors. Best +38.9R total; one config positive in all
  four years (+18.3R).
- **Out-of-sample:** every survivor negative. Best −10.5R (PF 0.72); shipped
  baseline −28.5R.

**In-sample edge did not transfer.** This is the textbook signature of overfitting,
and it is why "tune the filters until the curve is positive" was rejected as
methodology — on a 4-trade month it is curve-fitting, and on the full grid it
produced nothing that survived OOS.

## Test 3 — Two-year baseline (largest sample)

Rule-only walk-forward, **June 2024 → June 2026**, 24 monthly folds, no LLM.

| strategy | trades | win rate | return | max DD | Sharpe |
|---|---|---|---|---|---|
| rule-only ICT | 89 | 29.2% | **−15.45%** | −15.45% | −1.07 |
| buy-and-hold | — | — | **+5.95%** | −9.09% | +0.38 |

89 trades is a statistically meaningful sample. The 29.2% hit rate is fatally short
of the ~33% a 2R strategy needs to break even (PF 0.75 — returns 75¢ per dollar
risked). Max drawdown equals total return: it bled to its low at the endpoint with no
recovery. **Holding EUR/USD beat the "smart" rules by 21 percentage points.**

## Test 4 — Breakout/continuation family (opposite hypothesis)

The 2-year data hinted that this period rewarded trend exposure, so a second strategy
family was added to the harness: trade *with* a recent 15m displacement (momentum)
instead of fading a sweep, stop beyond the last opposing 15m swing. Same 864-config
grid, same tune (2021-2024) / validate-once (2025-2026) protocol.

| stage | result |
|---|---|
| in-sample | **47 robust survivors** (vs 16 for reversal); best +31.7R, PF 1.31, positive all 4 years |
| out-of-sample (best) | +2.3R, **PF 1.05**, win rate 43% |
| out-of-sample (top in-sample config) | +1.1R, **PF 1.02** — a ~95% decay from its +31.7R in-sample |

**This is a different result from reversal, but not a success.** Breakout held
*marginally positive* OOS (PF ~1.02-1.05) where every reversal config went firmly
negative (PF 0.53-0.72) — trend-continuation has a faint pulse where mean-reversion
was dead. But the in-sample edge decayed ~95%, and a PF near 1.03 on ~80 trades is
within noise of break-even; with only a 1-pip spread modeled, realistic slippage
would likely erase it. **Verdict: directionally interesting, not a tradeable edge.**

A follow-up probe added a 1:1 RR target (the grid had only tested 1.5/2/3). In-sample
the 1:1 breakout configs hit ~57% win rate (PF 1.2-1.25, above the 50% break-even a
1:1 target needs), but OOS the win rate decayed to ~48% and all finalists went
*negative* (PF 0.83-0.88) — worse than the 1.5R version. The lesson is structural:
at 1:1 there is no reward asymmetry to cushion the win-rate decay that overfitting
always produced, so it falls straight below break-even. Lower RR did not help.

## Test 5 — Proper-ICT HTF directional-bias filter

Web research into how ICT is actually specified (Silver Bullet, 2022 mentorship model)
surfaced the biggest gap in our rules: **no higher-timeframe bias filter.** Textbook ICT
forbids trading against HTF structure ("opposite to HTF trend = invalid setup"); our
rules faded sweeps in both directions regardless of the 1h/4h trend. Added an `htf_bias`
filter (trade only with the 1h or 4h trend) and re-screened both families (3,456 configs
each, same tune/validate protocol).

| | in-sample (2021-2024) | out-of-sample (2025-2026) |
|---|---|---|
| breakout, no bias | best PF 1.63 | PF 1.05, +2.3R / 79 trades |
| breakout, **bias 1h** | best PF **1.72**, win rate 55% | PF **1.12**, +1.4R / 20 trades |
| reversal, no bias | best PF 1.29 | PF 0.66 (**negative**) |
| reversal, **bias 1h** | best PF **1.60**, win rate 64% | PF **1.86**, +5.4R / **only 12 trades** |

**The filter genuinely helped, in the direction ICT theory predicts** — win rates rose to
55-64%, in-sample PF jumped, and OOS moved from break-even/negative toward marginally
positive. This is the first change that *improved* out-of-sample behavior. **But it is
still not a tradeable edge:** the filter is so selective it cut OOS samples to 12-20
trades — too thin to trust (one trade swings PF materially), the surviving edge (PF ~1.1)
is within noise, and only a 1-pip spread is modeled. The in-sample→OOS decay persists
(PF 1.7 → 1.1). Verdict: proper-ICT HTF alignment is a real, correct-direction
improvement that lifts the strategy from "loses money" to "roughly break-even," but does
not clear the bar for a deployable edge on this evidence.

---

## Conclusion

1. **The signal source is the problem, not the implementation.** ICT
   sweep-reversal on EUR/USD intraday has negative expectancy across a large,
   honest sample; the opposite (breakout/continuation) is at best break-even.
2. **An LLM cannot rescue a negative-expectancy signal.** Filtering bad trades
   harder does not make them good; the LLM would need to *be* the entire edge,
   and at 8B/3B local scale it was not.
3. **In-sample never survived out-of-sample.** Both families produced dozens of
   configs profitable across four tune years; reversal then went negative and
   breakout decayed to break-even on untouched data. On EUR/USD 15m, in-sample
   confluence performance was not predictive — a result worth knowing in itself.
4. **The framework is sound and reusable.** Leakage-safe engine, verified cost and
   risk modeling, and a screening harness that tune-and-validates a strategy family
   over four years in minutes.

**What would be productive next** (the harness makes each a fast screen): a less
efficient instrument (BTC data is in the repo); or a longer horizon (daily bars,
where a 1-pip spread becomes noise and trend persistence is stronger). More tuning of
these intraday EUR/USD families is not worth the compute.

*One window, one pair, paper costs — every result here carries that caveat. The
finding is "this strategy family failed every test we ran," not "no strategy can
work."*
