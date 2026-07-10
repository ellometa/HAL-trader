# ICT Tournament 2 — Build Spec

**Goal:** one HTML report comparing three *new* ICT models + two macro-filter variants
against the incumbent **SLIP-ADX** (Test 7's only OOS winner) + buy-and-hold, over the
**5-year** window (2021-06-06 → 2026-06-06). Stay on these rails.

## The contenders (fixed definitions — do not drift)

### 1. TURTLE SOUP  (new — ICT stop-hunt fade)
Sources: fxopen.com, innercircletrader.net, fluxcharts.com turtle-soup guides.
- Time gate: **02:00–11:00 ET** (London + NY-AM killzones).
- Reference levels: **previous completed daily bar's high/low** (PDH/PDL).
- Signal: the 15m bar closing at `t` **pierces** PDH (high > PDH) but **closes back
  below** it → short. Mirror for PDL → long. Sweep-and-reclaim in one bar.
- Stop: beyond the sweep extreme ± 2-pip buffer.
- **Target = previous-day equilibrium** ((PDH+PDL)/2) — a mechanization choice; ICT
  texts say "opposite side of the range", midpoint is the conservative reading.
  Variable RR; skip if < 1.0R. One attempt per level per ET day.

### 2. TRIPWIRE  (new — ICT Power of Three / AMD)
Sources: innercircletrader.net PO3, tradingfinder.com, fxopen.com PO3 guides.
- **Asia accumulation range:** 19:00 (prev ET evening) → 02:00 ET. Knowable from 02:00.
- Time gate: entries **02:00–11:00 ET** (London manipulation → NY-AM distribution).
- Bias = **1h trend** (same proxy all our strategies use).
- **Manipulation:** since 02:00 ET, price must have swept the Asia-range side
  *against* bias (bullish bias → raid below Asia low). 
- **Confirmation:** same-as-bias 15m structure shift *after* the sweep extreme.
- Enter with bias; stop beyond the manipulation extreme ± buffer; **TP = 1.5R fixed**.

### 3. SLIP-MACRO / SLIP-ADX-MACRO  (variants — ICT macro time-windows)
Sources: ictkillzone.com macro times, innercircletrader.net macro strategy.
- SLIPSTREAM entry rules, but entries only when the 15m decision close falls inside an
  **ICT macro window** (ET): 8:50–9:10, 9:50–10:10, 10:50–11:10, 11:50–12:10,
  13:10–13:30, 14:10–14:30, 15:15–15:45 (half-open).
- SLIP-ADX-MACRO stacks the macro gate on top of ADX≥22 — tests whether the two
  filters that each claim to remove noise are additive.

### Control: SLIP-ADX (adx_min=22, rr=1.5) — keyed `rule_ict` so report wiring works.

## Run configuration
- `BOT_COMPARE_ICT2=1 BOT_SESSION_START=02:00 BOT_SESSION_END=16:00 BOT_RR_FREE=1
  BOT_NO_BREAKERS=1 BOT_SKIP_LLM=1 BOT_START=2021-06-06 BOT_END=2026-06-06`
- No breakers (user standing instruction: show the raw 5-year trajectory).

## Honesty rules (non-negotiable)
- SLIP-ADX and the SLIP-* variants inherit SLIPSTREAM's tuning on 2021-2024 data —
  they are **favored in-sample**. TURTLE SOUP and TRIPWIRE use fixed ICT defaults,
  untuned. Say so in the verdict.
- **2025-01 → 2026-06 is the only unbiased comparison window.** Split it out.
- Costs: 1-pip spread only. Real costs reduce everything.
- Thin samples get flagged (SLIP-ADX's edge = 10 OOS trades; don't let a new
  10-trade winner be oversold either).
