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

## Test 6 — Three named strategies, 5-year head-to-head

Forum research (ForexFactory ICT threads, Judas Swing) produced two new strategies to
run beside the tuned one, over 2021-06 → 2026-06:
- **SLIPSTREAM** — breakout + 1h HTF bias, fixed 1.5R (the Test 5 winner; *tuned* on 2021-24).
- **MIDNIGHT RAID** — ICT Judas Swing: 00:00-05:00 ET, sweep the NY midnight open against
  1h bias, reverse on MSS, target previous-day high/low. *Fixed ICT defaults, untuned.*
- **BLOODHOUND** — SLIPSTREAM entry with previous-day-H/L liquidity targets. *Untuned.*

| strategy | full 5yr | in-sample 21-24 | **OOS 25-26** | note |
|---|---|---|---|---|
| SLIPSTREAM | +15.9% | +14.4% | **+1.3%** | tuned; gains are in-sample |
| BLOODHOUND | +7.4% | +7.2% | **+0.2%** | flat OOS |
| MIDNIGHT RAID | −8.0% | −8.0% | **dead** | hit 15% drawdown halt May 2022, never traded again |
| buy-and-hold | −5.3% | — | — | passive |

**Findings.** (1) The forum-celebrated **Judas Swing was the worst** — it self-destructed
into the drawdown halt in 11 months, the same month the original reversal rules did.
Forum popularity is not edge. (2) **Liquidity targets (BLOODHOUND) did not beat a fixed
1.5R** (SLIPSTREAM) — the ICT "target the opposing liquidity" claim underperformed a
simple ratio here. (3) Decomposed honestly, **all three are flat out-of-sample** (+1.3%,
+0.2%, dead); the 5-year headlines are entirely the favorable 2021-2024 trend regime,
not a durable edge. The verdict is unchanged across six tests and three strategy families.

## Test 7 — Improving SLIPSTREAM (first genuine OOS improvement)

Diagnosis of base SLIPSTREAM: (a) regime-dependent — *all* profit came from the 2023-24
trend years; (b) the **long side loses** (longs −0.05 avg R / 40% win vs shorts +0.40 /
60%). Reddit/SMC research suggested four levers, each tested as an isolated variant over
5 years (no breakers, full curves):

| variant | lever | full 5yr | in-sample | **OOS 25-26** | OOS trades | OOS win% |
|---|---|---|---|---|---|---|
| SLIPSTREAM | control | +15.9% | +14.4% | +1.3% | 20 | 45% |
| **SLIP-ADX** | skip chop (1h ADX>22) | +8.1% | +0.8% | **+7.2%** | 10 | **70%** |
| SLIP-RUNNER | 3R target | +13.4% | +11.0% | +2.1% | 19 | 32% |
| SLIP-BREAKEVEN | stop→entry at 1R | +9.4% | +9.0% | +0.4% | 20 | 35% |
| SLIP-SHORT | shorts only | +17.2% | +12.9% | +3.8% | 13 | 54% |
| SLIP-FUSION | 2R+ADX+short+BE | +6.8% | +3.0% | +3.7% | 7 | 43% |

**Findings.** (1) **The ADX trend-strength filter is the first change in the project that
worked *better* out-of-sample than in-sample** (+7.2% OOS vs +0.8% in-sample, 70% win) —
the opposite of the overfitting signature seen everywhere else. It cut trades ~half by
skipping chop, exactly as a real filter should. (2) **SLIP-SHORT confirmed the
diagnosis** — dropping the losing longs lifted every metric (best full-period PF 1.82,
Sharpe 0.76; +3.8% OOS). (3) **Break-even *hurt*** (+0.4% OOS) — it stops out trades that
later recover, as predicted. (4) RUNNER (3R) added drawdown without OOS benefit.

**Honest caveat.** These are encouraging but **thin** — SLIP-ADX rests on 10 OOS trades, a
70% win rate there has a wide confidence interval, and only a 1-pip spread is modeled.
This is the first real signal that an edge *might* exist (regime-filtered, short-biased
trend continuation), not proof of one. Next step would be confirming the ADX filter on
more data / a rolling walk-forward before any trust.

---

## Test 8 — ICT Tournament 2: Turtle Soup, Power of Three, macro windows

**Question.** Do the remaining big ICT folk models — the stop-hunt fade (Turtle Soup),
Power of Three (Asia-range manipulation), and macro time-windows — beat the incumbent
SLIP-ADX? (Spec: `TOURNAMENT2_SPEC.md`; run: 2021-06 → 2026-06, no breakers,
`BOT_COMPARE_ICT2=1`, session 02:00–16:00 ET.)

| Strategy | Trades | Final equity | In-sample avg R | OOS avg R (2025+) |
|---|---|---|---|---|
| SLIP-ADX (control) | 47 | **$108,073** | +0.03R (37 tr) | **+0.70R (10 tr)** |
| TURTLE SOUP (PDH/PDL sweep fade → prev-day eq) | 723 | $47,362 | −0.08R (536 tr) | −0.10R (187 tr) |
| TRIPWIRE (PO3: Asia raid → MSS with 1h bias) | 480 | $88,593 | −0.01R (358 tr) | −0.04R (122 tr) |
| SLIP-MACRO (SLIPSTREAM ∩ ICT macro windows) | 33 | $98,765 | −0.09R (22 tr) | +0.09R (11 tr) |
| SLIP-ADX-MACRO | 18 | $102,166 | −0.19R (13 tr) | +0.95R (5 tr) |
| buy-and-hold EUR/USD | — | $94,725 | — | — |

**Findings.** (1) **Turtle Soup is decisively negative** — and unlike everything else in
this project, the verdict rests on 723 trades with *consistent* IS and OOS behavior
(26–27% win both halves). Median stop is only 9 pips, so the 1-pip spread alone costs
~0.11R/trade: the gross pattern is a coin flip (≈+0.02R gross) and costs turn it into a
fee machine. The "failed sweep reverses" claim has **zero predictive content** here.
(2) **Power of Three is flat-negative on 480 trades** (≈0R gross, minus costs) — same
verdict, robustly measured. (3) **Macro windows subtract value**: gating SLIPSTREAM to
the "algorithm delivery" slots deleted trades without improving them (SLIP-MACRO worse
than SLIPSTREAM; SLIP-ADX-MACRO worse than SLIP-ADX on totals; its +0.95R OOS is 5
trades — noise). (4) SLIP-ADX stays champion.

**The meta-result is the sample-size asymmetry.** The models that trade often enough to
measure properly (723 and 480 trades) show **no gross edge whatsoever** — the honest,
high-confidence readout of mechanized ICT on EUR/USD 15m. The only "winner" is the one
that trades so rarely (10 OOS trades) that its record could still be luck. Nothing in
this tournament produced a reason to believe the next rare-trade winner either.

---

## Test 9 — Evidence-backed pivot: replicate published strategies verbatim

**Question.** Leaving ICT and EUR/USD behind: do the strategies with *published, audited*
backtests (QuantifiedStrategies, Connors, Grayscale, Quantpedia) replicate on our own
independently fetched data, with our own code and honest costs? Zero tuning by us —
rules verbatim (`pivot_screen.py`). Sub-period 2025-01+ post-dates the publications
(closest thing to OOS a replication has).

| Strategy | Full period (net) | 2025+ | Published | Verdict |
|---|---|---|---|---|
| SPY Turnaround Tuesday | 9.2%/yr, Shp 0.93, −15% DD, 631 tr | **14.9%/yr, Shp 1.33** | 7.9%/yr, 75% win | **replicates** |
| SPY RSI-2 + 200MA (Connors) | 5.1%/yr, Shp 0.81, 11% expo | 6.8%/yr, Shp 1.19 | book rules | **replicates** |
| SPY RSI-2 plain | 8.7%/yr, −34% DD, 28% expo | 10.7%/yr | 9%/yr, −34% DD, 28% expo | **near-exact** |
| SPY End-of-month | 6.8%/yr | 7.3%/yr | 6.4%/yr | replicates |
| BTC 20/100 MA (Grayscale) | 38.9%/yr, Shp 0.99 (HODL 0.85) | −9.7%/yr (BTC −26%) | Shp 1.7 vs 1.3 | direction only |
| BTC D1H1 MACD (Quantpedia) | **−20.3%/yr**, PF 0.84, 948 tr | −23%/yr | +10.8%/yr, Shp 1.07 | **fails (cost churn)** |
| SPY buy & hold | 10.9%/yr, Shp 0.65, −55% DD | 19.8%/yr | — | benchmark |

**Findings.** (1) **The equity anomalies are real and replicate** — RSI-2 landed within
0.3 points of the published CAGR with identical exposure and drawdown; Turnaround
Tuesday *improved* after publication. 340–631 trades over 33 years, near-zero
parameters. (2) **None beat buy-and-hold on raw CAGR; all beat it on risk** — TT earns
~85% of buy-hold's return in 22% of the time with a quarter of the drawdown. Real edges
are risk-shaped, not return-shaped. (3) **The harness caught a fake:** the BTC hourly
MACD claim dies under 10 bps/side (948 trades of churn). (4) Daily bars + liquid
equities is where published edges live; intraday FX (Tests 1–8) is where they don't.

Data: SPY 1993–2026 / QQQ 1999–2026 (yfinance, dividend-adjusted, 1 bp/side);
BTCUSDT 2020–2026 (10 bps/side). The paywalled QQQ day-of-week strategy was not
replicated — we don't guess at rules and call it replication.

---

## Test 10 — Portfolio, stress battery, and the funded-account answer

**Question.** Stack the Test 9 survivors into one SPY portfolio (RSI-2+200MA,
Turnaround Tuesday, End-of-month, IBS, Double 7s — all published rules, zero tuning),
stress it four ways, and Monte-Carlo an FTMO Swing $100k challenge with it
(`stress_funded.py`, report `stress_funded_*.html`).

**The portfolio.** PORTFOLIO avg: **CAGR 8.4%, Sharpe 1.14, MaxDD −10%** over 33 years —
top Sharpe of everything tested in this project; **Sharpe 1.61 post-2025**. Union
variant: 13.5%/yr (beats buy-and-hold's 10.9%) at −30% DD vs −55%. Frequency: 89
signal-changes/yr (avg) / 23 round-trips/yr (union), ~70% exposure — the "6 trades a
year" problem is solved by stacking non-overlapping anomalies, not by trading faster.

**Stress battery.** S1 *plateau:* all 17 parameter neighbors profitable (worst Sharpe
0.59, most ≥0.75) — edge is a plateau, not a spike. S2 *bootstrap* (2,000×2yr paths):
median CAGR +8.4%, P(2yr loss) 4.1%, MaxDD 95th-pct −11.2%. S3 *decades:* Sharpe
0.88–1.42 in every era 1993–2026, DD never worse than −10%. S4 *costs:* survives 10
bps/side (Sharpe 0.79).

**FTMO Swing $100k Monte Carlo** (fee $540, +10%/+5% targets, −5% daily / −10% total
static, 80% split, 3,000 paths × 4yr):

| leverage | pass rate | med. days→funded | funded acct dies | EV net of fee | ≈$/month |
|---|---|---|---|---|---|
| 1x | 92.7% | 393 (~19 mo) | 8.4% | **+$13,688** | $299 |
| 2x | 64.2% | 159 (~8 mo) | 86.4% | +$13,838 | $302 |
| 3x | 52.8% | 93 | 99.3% | +$9,110 | $203 |
| 5x | 34.4% | 41 | 100% | +$2,896 | $72 |

**Findings.** (1) The challenge is passable with high probability — but slowly: at
safe leverage the median path takes ~1.5 years to get funded. (2) Leverage buys speed
and pays for it with near-certain account death (the −5% daily line is unsurvivable at
3x+ given SPY's tail days). (3) EV is positive at every leverage — the fee is cheap
relative to payouts — and maximized around 1–2x at **≈$300/month expected per $100k
account**. (4) Honest caveats: daily-loss breaches checked close-to-close only
(intraday spikes → real bust risk is higher); bootstrap assumes the 33-year return
distribution persists; one strategy family (long-only US equity mean reversion) —
correlated across any number of accounts, so N accounts ≠ N independent incomes in a
crash.

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
