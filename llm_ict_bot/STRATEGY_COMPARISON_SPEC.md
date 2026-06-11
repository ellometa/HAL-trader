# Three-Strategy ICT Comparison — Build Spec

**Goal:** one HTML report comparing three named ICT strategies + buy-and-hold over a
**5-year** window (2021-06-06 → 2026-06-06), no progress bar. Stay on these rails.

## The three strategies (fixed definitions — do not drift)

### 1. SLIPSTREAM  (existing — breakout + HTF bias; the Test 5 winner)
- Most recent 15m **displacement** within 2h sets direction.
- **1h trend** must agree (HTF bias).
- Same-direction 15m **structure shift** after the displacement.
- **4h premium/discount** filter (long only in discount, short only in premium).
- Stop beyond the last opposing **15m swing**; **TP = 1.5R** (fixed).
- Time gate: NY session **07:00–16:00 ET**.
- Already implemented as `make_breakout_decide_fn`.

### 2. MIDNIGHT RAID  (new — ICT Judas Swing)
- Time gate: **00:00–05:00 ET** only.
- Daily **bias = 1h trend** (proxy for ICT daily bias).
- Mark the **NY midnight (00:00 ET) open** price for the current day.
- **Judas sweep:** price spikes *against* bias through the midnight open, then a
  same-as-bias 15m **structure shift** confirms the reversal.
- Enter in the bias direction; **stop beyond the judas extreme** (swept wick ±2 pips).
- **Target = previous-day high** (bullish bias) / **previous-day low** (bearish bias)
  — a *liquidity* target, so RR is variable. Skip if that target gives < 1.0R.

### 3. BLOODHOUND  (new — SLIPSTREAM entry, liquidity target)
- **Identical entry** to SLIPSTREAM (displacement + 1h bias + structure + P/D).
- **TP = previous-day high/low** (liquidity) instead of fixed 1.5R; variable RR.
  Skip if the liquidity target gives < 1.0R.
- Stop beyond the last opposing 15m swing.
- Time gate: NY session 07:00–16:00 ET.

## Engine changes required
- `BOT_SESSION_START/END` env-overridable session gate; set 00:00–16:00 for the
  compare run so MIDNIGHT RAID's window is reachable; each strat self-gates its own
  sub-window internally.
- `BOT_RR_FREE=1` relaxes the validator's fixed-RR check (liquidity targets are
  variable-RR); still enforces SL<entry<TP and entry-drift.
- Point-in-time helpers (no leakage): `prev_day_hilo(t)` from the completed 1d bar;
  `midnight_open(t)` from the current day's 00:00 ET 15m/1m open.
- `BOT_COMPARE_STRATS=1` runs all three into `RESULTS` (+ buy_hold) so the existing
  report plots/table compare them automatically.

## Honesty rules (non-negotiable)
- The window is 5 years; **2021-06 → 2024-12 overlaps SLIPSTREAM's tune data**, so
  SLIPSTREAM is favored in-sample. MIDNIGHT RAID and BLOODHOUND use **fixed ICT
  defaults (no tuning by us)**. Note this asymmetry in the report verdict.
- Report the **2025-01 → 2026-06 out-of-sample** sub-period separately if feasible;
  that is the only unbiased comparison.
- Costs: only 1-pip spread modeled. Flag that real costs would reduce all three.

## Deliverable
- `BOT_COMPARE_STRATS=1 BOT_SESSION_START=00:00 BOT_SESSION_END=16:00 BOT_RR_FREE=1
  BOT_SKIP_LLM=1 BOT_START=2021-06-06 BOT_END=2026-06-06` → execute notebook → open
  the HTML report (equity overlay of all 3 + buy-hold + S&P, drawdown, metrics table).
- No `BOT_PROGRESS`.
