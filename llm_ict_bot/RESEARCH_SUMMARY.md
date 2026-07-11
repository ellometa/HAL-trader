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

## Test 11 — FX funded-account simulation: SLIP-ADX vs 2-step evals

**Question.** Buy a $100k FX prop eval and trade it with SLIP-ADX — when does it pass,
when does it blow? (`fx_funded_sim.py`.) Rules researched per firm: FTMO Swing
(+10%/+5%, −5% daily / −10% static, min 4 days, $540, 80%), FundingPips (+8%/+5%,
$399, 80%), FundedNext (+10%/+5%, $549, 90%). Trade stream bootstrapped from the real
47-trade walk-forward record (median 28 days between trades); three edge scenarios:
**oos** (the 10 OOS trades, +0.70R — optimistic), **full** (all 47, +0.17R — base),
**null** (demeaned — skeptic). 5,000 paths × 5 years per cell.

Representative cells (FundingPips, the friendliest rules):

| edge | risk/trade | pass | med. months→funded | blow (5yr) | med. months→blow | EV (5yr) |
|---|---|---|---|---|---|---|
| oos | 1% | 99.1% | 24 | 0.1% | — | +$14,990 |
| oos | 2% | 99.0% | 12 | 6.6% | 38 | +$39,743 |
| **full** | **2%** | **62.1%** | **23** | **55.3%** | **31** | **+$5,891 (~$105/mo)** |
| full | 1% | 33.6% | 41 | 5.7% | 41 | +$795 |
| null | 2% | 31.9% | 25 | 75.4% | 26 | +$1,512 |
| null | 3% | 32.0% | 15 | 96.6% | 15 | +$2,269 |

**Findings.** (1) **Everything hinges on which edge you believe.** If the 10-trade OOS
record is real, evals are near-free money (99% pass, EV $15–60k). On the defensible
full-record base (+0.17R), it's a coin-flip business: ~60% pass in ~2 years, ~55%
eventually blow, ~$100/month expected. (2) **How it blows:** never the −5% daily line
(one position at ≤3% risk can't reach it) — always the slow bleed to −10% max-loss,
median ~2–3 years in. (3) **The null-edge EV is *positive* at high risk** (+$1.5–2.3k)
— that's the fee-vs-payout optionality of the prop model itself, which firms suppress
in practice via consistency rules, payout reviews, and denials. Don't bank on it.
(4) **100% of paths trip a >30-day inactivity gap** — SLIP-ADX's 28-day median trade
spacing means every firm's inactivity policy gets tested; FTMO allows freezes,
others may terminate. A ~monthly-trading strategy is structurally awkward for props.
(5) Comparison: the SPY portfolio route (Test 10) pays ~3× more per account-month at
a 93% pass rate. FX remains the worse vehicle, exactly as Tests 1–8 kept saying.

---

## Test 12 — The eval lab: strategies engineered explicitly to pass funded evals

**Question.** Optimize for the eval objective itself — hit +8-10% before −10%, fast,
then survive — rather than Sharpe (`eval_lab.py`). Candidates re-engineered from the
Test 9 replicated legs: SPY portfolio (baseline), QQQ portfolio (speed via vol),
MULTI 50/50 (drawdown room via diversification), MULTI + 10% vol-targeting (tail
suppression), TT alone (speed benchmark). Excluded on published evidence: XAUUSD mean
reversion (negative, 8,693-trade study) and martingale/grid "pass-service" EAs
(banned + blow by design). 3,000 paths × 4yr, leverage swept 1–4×.

Best leverage per candidate (FundingPips +8%/+5%, $399):

| candidate | lev | pass | med months→funded | funded death | EV (4yr) | $/month |
|---|---|---|---|---|---|---|
| **SPY portfolio** | **1.5×** | **82.2%** | **9.8** | 49.9% | **+$19,817** | **$425** |
| MULTI 50/50 | 1.0× | 82.2% | 12.8 | **37.6%** | +$15,002 | $323 |
| MULTI vol-target | 1.0× | 74.0% | 10.0 | 64.6% | +$13,419 | $290 |
| SPY TT alone | 1.0× | 77.2% | 13.3 | 48.2% | +$13,410 | $290 |
| QQQ portfolio | 1.0× | 62.7% | 8.6 | 84.1% | +$10,293 | $225 |

**Findings.** (1) **The eval game is won by drawdown room, not speed.** QQQ's extra
volatility bought only ~1 month faster funding but doubled funded-account mortality
(84% vs 50%) — its −34% historical MaxDD (dot-com era) is fatal inside a −10% budget.
(2) **The winner is still the SPY portfolio, just at 1.5× leverage: $425/month
expected, 82% pass, funded in ~10 months median** — +42% income over Test 10's 1×
number; the 1.5× sweet spot sits between Test 10's integer scan points. (3) **Vol
targeting failed here** — honest negative: it levers up calm regimes right before vol
spikes, and de-levering high-vol periods cuts exactly the days mean reversion earns
its keep. (4) Firm choice is second-order (FundingPips ≈ FTMO ± ~5%). (5) MULTI 50/50
is the conservative pick — highest funded survival (62% alive at 4yr) for ~25% less
income.

---

## Test 13 — Harness audit + the bond sleeve (FORTRESS)

**Part A — robustness audit of the Test 9–12 code** (`test_harness_sanity.py`).
Five executable invariants, 13 checks, all pass: **truncation invariance** (deleting
the future changes no position — the definitive lookahead test, run per strategy);
accounting identity (pos=1 at zero cost reproduces close[-1]/close[0] to 1e-9); cost
monotonicity + hand-checked round-trip drag; bootstrap fidelity (mean/std converge);
eval-sim limiting behavior (always-up passes 100%, always-down blows 100%, EV=-fee).
Bootstrap block-size sensitivity on the headline number: pass 82–85%, $421–444/mo
across blocks 5→42 — the 5-day-block concern does not move the answer. Known
simplifications that remain: end-of-month uses the month's trading-day count
(exchange-calendar knowledge, not price lookahead); daily-loss checked close-to-close;
FundingPips balance-carry approximated as reset.

**Part B — more strategies: the bond sleeve.** Test 12 showed QQQ adds correlation,
not diversification. The published month-end bond effect (window-dressing flows into
Treasuries; QS "Seasonal Strategy for Bonds") + IBS on TLT form a bond sleeve
(TLT 2002–2026, fetched via yfinance). **FORTRESS = 70% SPY portfolio + 30% TLT
sleeve**:

| profile | CAGR | Sharpe | MaxDD | return/DD |
|---|---|---|---|---|
| SPY portfolio | 8.4% | 1.14 | −9.7% | 0.86 |
| **FORTRESS 70/30** | 7.2% | **1.41** | **−6.6%** | **1.10** |

Eval results (FundingPips, best leverage): **FORTRESS at 2×: 88.8% pass, 9.3 months
to funded, 39.9% funded death, $544/month** — beats the SPY portfolio's $425/mo on
every dimension simultaneously. The −6.6% MaxDD buys the leverage room that QQQ's
volatility could not. TLT sleeve alone is mediocre (~$220/mo) — its value is entirely
in the combination.

**Caveats.** TLT data starts 2002 (no 1970s-style rate-shock regime in sample; 2022
*is* in sample and FORTRESS's −6.6% MaxDD includes it). The month-end bond effect has
mixed independent replications — our own implementation of the conservative window is
what's tested here. Same correlated-accounts caveat as ever.

---

## Test 14 — More data: 64-year validation + intraday-aware limits

**A · ^GSPC 1962–2026 (16,238 days).** The five legs re-run on the S&P index itself,
split at 1993 (`more_data_check.py`):

| leg | 1962–92 CAGR / Sharpe | 1993–2026 CAGR / Sharpe |
|---|---|---|
| RSI-2 + 200MA | −0.0% / 0.01 | +4.3% / 0.71 |
| Turnaround Tuesday | −2.0% / −0.22 | +7.9% / 0.81 |
| IBS | −1.6% / −0.10 | +8.6% / 0.71 |
| Double 7s | −0.8% / −0.05 | +7.0% / 0.76 |
| **End-of-month** | **+6.7% / 0.85** | **+6.4% / 0.65** |
| PORTFOLIO avg | +0.6% / 0.14 | +7.1% / 1.01 |

**The short-term mean-reversion family did not exist before ~1990.** Only the
end-of-month flow effect is era-invariant (64 straight years). The mean-reversion
edge is a *regime* — born with index arbitrage/program trading in the late 80s,
stable for ~35 years including post-publication — not a law of nature. (Softener:
the index carries no dividends; pre-1993 yields of 3–5% would add roughly +1–2%/yr
at these exposures — the era gap narrows but does not close.) A regime that
switched on can switch off; the portfolio's edge should be presumed mortal and
monitored, not trusted indefinitely.

**B · Intraday-aware eval limits.** Daily lows bound the worst intraday mark
(conservative: full position at every instrument's simultaneous low). Reshuffles the
leaderboard (FundingPips):

| candidate | close-only | intraday-aware |
|---|---|---|
| FORTRESS 2.0× | 88.8% pass, $544/mo | 78.0% pass, 66% death, $416/mo |
| **FORTRESS 1.5×** | ~95% pass | **94.9% pass, 12.4 mo, 15.4% death, $486/mo** |
| FORTRESS 1.0× | 97%+ | 97.5% pass, 1.0% death, $278/mo |
| SPY portfolio 1.5× | 82.2%, $425/mo | 71.0% pass, 68.5% death, $334/mo |

**2× leverage was an artifact of ignoring intraday marks** — the −5% daily line gets
touched intra-day on tail days. **FORTRESS at 1.5× is the robust optimum: ~$486/month
and 94.9% pass even under the conservative bound** (truth lies between the columns).

---

## Test 15 — The strategy family vs actual EUR/USD tick data

**Question.** Do the FORTRESS legs work on forex? Run on the repo's own tick-derived
EUR/USD daily bars (4.3 GB of bid/ask ticks, 2013–2026; chain verified: 1m-rebuilt
daily bars match stored daily bars with 0.0 deviation on H/L/C).

| leg | 2013–26 CAGR / Sharpe | 2021–26 CAGR / Sharpe |
|---|---|---|
| RSI-2 plain | −0.3% / −0.05 | −0.4% / −0.06 |
| RSI-2 + 200MA | −0.5% / −0.24 | −1.0% / −0.46 |
| Turnaround Tuesday | −0.0% / 0.02 | +1.1% / 0.29 |
| End-of-month | −0.9% / −0.19 | −1.3% / −0.27 |
| IBS | +0.6% / 0.14 | +1.0% / 0.23 |
| Double 7s | +0.9% / 0.27 | +0.2% / 0.08 |
| **PORTFOLIO avg** | **+0.1% / 0.04** | **+0.1% / 0.03** |
| buy & hold EUR/USD | −1.0% / −0.09 | −1.1% / −0.11 |

**Finding.** The anomaly family is **dead on EUR/USD** — every leg within noise of
zero, portfolio Sharpe 0.04. Perfectly consistent with Test 14 (the edge is an
equity-microstructure regime: month-end fund flows, index-arb mean reversion) and
with Tests 1–8 (FX yields nothing). FORTRESS's numbers do NOT transfer to forex;
a funded account trading it must trade US500/US100-style index instruments, not
currency pairs. Method note: every number in Tests 9–15 is computed by our own
audited engine on locally-held price data — published stats were used only to
select candidates, never as results.

---

## Test 16-prep — Bot mining: what other trading bots have that we don't

Surveyed four open-source ecosystems while the gold tick download ran
(`bot_mining.py`): **freqtrade-strategies** (hyperopt-fit TA mashups, backtested on
20 days of 2018 — the overfitting signature our Tests 2-4 documented; nothing to
take beyond the protections already ported in Test 10), **jesse examples** (same
family), **je-suis-tm/quant-trading** (17 strategies, frictionless backtests; one
transferable), **QuantConnect community library** (academic replications — source
of the main candidate). Two survivors, both tested on our data:

| candidate | result | verdict |
|---|---|---|
| **GEM dual momentum** (Antonacci; SPY/EFA/AGG + BIL filter, monthly) | 10.1%/yr, Sharpe 0.66, −34% MaxDD, 1.9 switches/yr — era-consistent, matches published character incl. the documented 2022 whipsaw drawdown | **Replicates, but does not improve the stack**: +0.44 correlation with FORTRESS (both long-US-beta), and the 50/50 blend (Sharpe 1.08, −17.7% DD) is strictly worse than FORTRESS alone (1.41, −6.6%) |
| **Dual Thrust** (opening-range breakout, k1=0.5, 4d range) on BTC 15m | 8.9%/yr full, Sharpe 0.65 — but **−11%/yr, Sharpe −1.01 post-2025** | Fails OOS; no take |

**Conclusion.** The open-source bot world divides into (a) overfit TA mashups and
(b) academic replications — and our Test 9–13 stack already contains the best of
category (b) for our purposes. The one structural idea worth keeping on the shelf:
GEM-style absolute-momentum filters as crash protection at multi-year horizons —
irrelevant inside a −10% eval budget, potentially relevant for personal capital.

---

## Test 16 — ICT on gold: Turtle Soup, Judas, MMXM vs the most ICT-traded market

**Question.** Gold is the ICT community's favorite instrument — if the stop-hunt
narrative works anywhere, it's XAUUSD. Fresh 5-year tick-derived data (Dukascopy,
2021-06 → 2026-06, 2.26M 1m bars, 99.9% coverage; candle fast-path `fetchm1` after
the cold-CDN tick route degraded). Engine made instrument-agnostic
(BOT_SYMBOL/BOT_PIP/BOT_SPREAD; gold pip $0.10, spread $0.30). New model: **MMXM**
(Market Maker Buy/Sell Model — consolidation → liquidity run → SMR at HTF array →
return to origin), strict + loose mechanizations. SLIP-ADX rode along as the
cross-instrument check of its thin FX edge.

| strategy | trades | final equity | IS avg R | OOS avg R (2025+) |
|---|---|---|---|---|
| TURTLE SOUP | 674 | **$38,620 (−61%)** | −0.108R (25% win) | −0.150R (19% win) |
| MIDNIGHT RAID | 642 | $92,292 | −0.046R | +0.149R (157 tr) |
| MMXM-STRICT | 378 | $92,201 | −0.046R | +0.102R (83 tr) |
| MMXM-LOOSE | 456 | $72,454 | −0.120R | +0.133R (103 tr) |
| SLIP-ADX | 39 | $96,870 | −0.009R | **−0.262R (10 tr)** |
| **buy & hold gold** | — | **$235,871 (+136%)** | — | — |

**Findings.** (1) **Every ICT model lost money on gold across 5 years — during
gold's greatest bull market.** Doing nothing made +136%; the best ICT strategy made
−3%. (2) **Turtle Soup is now buried on two instruments** (723 EUR/USD + 674 gold
trades, −0.08 to −0.15R everywhere): fading stop-hunts has negative expectancy,
robustly measured. (3) **SLIP-ADX's cross-instrument check failed** (−0.26R OOS on
gold) — the oldest open question is answered: the 10-trade EUR/USD OOS record was
luck, not an edge. (4) The one flicker: MIDNIGHT RAID and MMXM turned positive in
the 2025+ sub-period (+0.10 to +0.15R on 83–157 trades) — but all three were
negative in-sample on 3× the trades, the OOS window coincides with gold's parabolic
2025-26 leg (long-bias artifacts), and after 16 tests the prior on "this time it's
real" is low. Not tradeable without a fresh confirmation window. (5) The ICT
catalog is now complete: **seven models, two instruments, ~3,900 trades — no
positive expectancy anywhere.**

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
