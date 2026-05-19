# Detector expansion — what to add next

Unlike the other docs in this folder, this one isn't an "undo
candidate." It's an *additive* ideation: the current detection layer
(FVG, OB, BOS/CHoCH, liquidity sweep) is healthy, and the question is
where to extend it. Same disclaimer applies — nothing here ships
without a real phase plan.

The current 4 detectors cover **structure**, **imbalance**, and
**liquidity grabs**. They don't cover:

- **Spatial context** — where is price *inside* the current range?
- **Temporal context** — which session formed this event?
- **Reference levels** — PDH, PDL, weekly opens, the numbers traders
  actually point at.
- **Zone-state evolution** — what a violated FVG or OB *becomes* after
  it gets taken out.

Each gap below is one detector. Tier 1 is what to ship first.

## Acceptance criteria for any new detector

Four bars to clear, in order. Failing any one of them is a strong
signal the idea isn't worth shipping.

1. **Computable from OHLC alone.** No order flow, no DOM, no volume
   profile. Volume is fine (we have it in the DataFrame) but most ICT
   is volume-agnostic and the Pine mirrors stay simpler without it.
2. **Composes with existing detectors.** A detector that *only* makes
   sense alone is worth less than one that lets the model reason
   across detections. "EQH was swept" is worth more than "EQH exists."
3. **Pine-mirrorable in ≤100 lines.** Your Pine mirrors are the
   correctness audit. If the Pine version would be horrible, the
   Python version is probably overfit to a niche case.
4. **Adds a question the model can't currently answer.** "Is price in
   premium?" is new. "Detect a hammer candle" is not — chartist noise,
   no ICT lineage.

---

## Tier 1 — cheap wins, high signal (ship first)

### 1. Equal Highs / Equal Lows (EQH/EQL)

The highest-ROI single add. EQH/EQL are clusters of swing highs/lows
within a small tolerance — pools of resting stops that the market
attacks before reversing. Real ICT trading revolves around them.

**Algorithm.** Reuse `find_swings(df, n)` from `structure.py`. Walk
swing highs in order; group adjacent swings whose price delta is below
`tolerance` (either an ATR multiple or a tick-based threshold,
configurable). Same for swing lows. Emit one event per cluster.

**Output schema:**
```python
{
    "kind": "eqh" | "eql",
    "level": float,                       # cluster centroid
    "tolerance_used": float,
    "member_times": [iso, iso, ...],
    "member_prices": [float, float, ...],
}
```

**Why this composes.** It composes directly with the existing
`liquidity_sweeps` detector. Today the model gets "a sweep happened at
T." After EQH/EQL it gets "the sweep took out a 3-touch EQH formed
Mon–Wed." That's a qualitative leap, not a quantitative one.

**Effort.** ~80 LOC + tests + Pine mirror. Pine version is nearly
trivial because `ta.pivothigh` already gives the inputs.

---

### 2. Premium / Discount zones with OTE band

Spatial context. The model currently knows *what* is on the chart but
not *where in the range* price is.

**Definition.** Within the current dealing range (most recent swing
high → swing low), the midpoint is 50%. **Discount** is below 50%
(institutional buy zone), **Premium** is above (institutional sell
zone). **OTE** is the 62–79% retracement band; 70.5% is the sweet
spot. Sourced from the ICT canon (`innercircletrader.net`).

**Algorithm.**
```python
def detect_pd_zones(df, lookback_swings_n=2):
    swings = find_swings(df, n=lookback_swings_n)
    hi = last_swing(swings, kind="high")
    lo = last_swing(swings, kind="low")
    direction = "bullish" if hi.idx > lo.idx else "bearish"
    rng = hi.price - lo.price
    mid = lo.price + 0.5 * rng
    ote_lo, ote_hi = lo.price + 0.62 * rng, lo.price + 0.79 * rng
    last = df["close"].iloc[-1]
    return {
        "direction": direction,
        "swing_high": hi.price, "swing_low": lo.price,
        "midline_50": mid,
        "ote_band": [ote_lo, ote_hi],
        "current_close": last,
        "zone": "premium" if last > mid else "discount",
        "in_ote": ote_lo <= last <= ote_hi,
    }
```

**Schema note.** This is a single *current state* object, not a list
of historical events. Slightly different shape than the existing
detectors. The model gets to ask "am I in premium?" — single
boolean-grade answer.

**Effort.** ~60 LOC. Pine mirror is a few `line.new` calls + a label.

---

### 3. Previous Day / Week High & Low (PDH/PDL/PWH/PWL)

The most-referenced liquidity in real ICT trading. The model should
know these numbers without being asked.

**Algorithm.** Resample the intraday DataFrame to daily / weekly OHLC
(you already do this for stocks → 4h resample in `ohlc.py`). Read the
**last completed** daily and weekly high+low. Emit four numbers plus
their timestamps.

**Output schema:**
```python
{
    "pdh": {"price": float, "time": iso},
    "pdl": {"price": float, "time": iso},
    "pwh": {"price": float, "time": iso},
    "pwl": {"price": float, "time": iso},
}
```

**Why this composes.** With EQH/EQL and sweep detection, the model
identifies *which* liquidity got taken: "the 4h sweep at 14:30 NY took
out yesterday's high." Interview-grade reasoning.

**Effort.** ~40 LOC. Pine mirror is a single `request.security` lookup
per level.

---

### 4. Killzone tagging

Not a standalone detector — a *tagging utility* applied to every other
detection. Time-of-day context for free.

**Windows (NY local time):**
- Asian: 19:00–22:00
- London: 02:00–05:00
- NY AM: 07:00–10:00 (forex), 08:30–11:00 (indices)
- NY Lunch: 12:00–13:00 (avoid)
- NY PM: 13:30–16:00

**Algorithm.** Add `backend/ict/sessions.py` with `killzone_for(ts)`
and `is_silver_bullet_window(ts)` helpers. Convert UTC → NY local
(`zoneinfo.ZoneInfo("America/New_York")`). In `detector.py`, after all
detectors run, stamp `event["killzone"] = killzone_for(event["start_time"])`
onto every output.

**Why this composes.** Every existing detection becomes more
informative without changing its shape. "Bullish OB formed during
London killzone" reads very differently than "bullish OB formed."

**Effort.** ~50 LOC. Pine has built-in session functions; the mirror
is one or two lines per highlighter.

---

## Tier 2 — medium effort, strong signal

### 5. Displacement gate

Not a new detector — a *quality flag* added to existing FVG and OB
outputs. Real ICT setups require **displacement**: an impulsive move,
not drift.

**Definition.** A candle (or 2–3 candle sequence) whose range exceeds
`N × ATR(period)`, typically N = 1.5–2.0. Body-dominant close (close
near the extreme) is a stricter variant.

**How to integrate.** Don't make this its own file. Add an `is_displaced`
flag and an `atr_multiple` field to each FVG / OB event:
```python
{
    "kind": "bullish_fvg", "start": ..., "end": ...,
    "displaced": True, "atr_multiple": 2.4,
}
```
The model can then prefer displaced setups. You also unlock filtered
variants later ("show only displaced OBs").

**Effort.** ~50 LOC: an ATR helper + a small change to `fvg.py` and
`order_blocks.py`. Pine has `ta.atr` built-in.

---

### 6. Breaker Block (BB)

A failed OB that flipped polarity. Per the ICT canon
(`tradingstrategyguides.com`, `theicttrader.com`): a bullish OB that's
violated (close beyond it in the opposite direction) becomes a bearish
breaker on re-test.

**Algorithm.** Extend the OB state machine. Today each OB is
`{active, mitigated}`. Add:
- `violated` — close beyond the OB in the opposing direction
- `breaker_confirmed` — after violation, price returns to the zone and
  rejects (close back through)

Emit a parallel `breaker_blocks` list with the *flipped* polarity.

**Effort.** ~120 LOC because of the state transitions on existing OBs.
Pine mirror is the same logic translated to box state.

---

### 7. Inversion FVG (IFVG)

Same idea applied to FVG. A bullish FVG that's violated becomes a
bearish IFVG — same level acts as resistance on re-test.

**Algorithm.** Extends the existing FVG mitigation tracking. After
mitigation, watch for close-through; if it happens, the FVG becomes an
IFVG with inverted polarity. Emit a parallel `inversion_fvgs` list.

**Effort.** ~80 LOC. Cleaner than breaker blocks because FVG state is
simpler (no BOS dependency).

---

### 8. Mitigation Block

A different beast from your current OB *mitigation*. A **mitigation
block** is the candle range that gets re-tested after a CHoCH, marking
where institutions are unwinding their original positions.

**Algorithm.** After a CHoCH event from `structure.py`, look back to
the last opposing-direction candle before the move. That's the
mitigation block. Closest cousin to existing OB logic but anchored to
CHoCH rather than BOS.

**Effort.** ~70 LOC. Could fold into `order_blocks.py` with an
`anchor_event: "bos" | "choch"` flag, or split into its own file. The
fold-in is cleaner.

---

## Tier 3 — narrative gold for an interview

### 9. Silver Bullet window
A specific ICT setup: FVGs that form inside 10:00–11:00 NY. Composes
with #1 + #4 — no new geometry, just a time-of-day filter on existing
FVG output. ~20 LOC.

### 10. Market Structure Shift (MSS)
A stricter CHoCH that *also* requires displacement. A subset of
existing CHoCH events flagged with the displacement gate from #5. ~10
LOC if #5 is done.

---

## What I'd skip and why

- **Power of 3 / AMD** — full daily session classification is
  pattern-matchy and brittle. Hard to ground-truth and easy to argue
  with in an interview. Skip until v2.
- **Quasi-modo / QM** — chartist territory. Subjective edges.
- **True Day Open** — easy to compute (00:00 NY) but interpretively
  thin on its own. Maybe later, as a one-liner.
- **Asian Range as a standalone object** — killzone tagging + PDH-style
  levels already cover what you'd want from it.
- **Order flow / volume profile** — out of scope. Not in OHLC.

---

## Recommended ordering

**Phase A (one focused pass):** Tier 1, in this order.

1. EQH/EQL → biggest composability win.
2. PD zones + OTE → unlocks spatial reasoning for the model.
3. PDH/PDL/PWH/PWL → completes the reference-levels picture.
4. Killzone tagging → enriches everything for almost no code.

Stop and assess. After this you've added 4 detectors, ~230 LOC, made
all detections composable, and given the model spatial + temporal +
liquidity-pool context. Coherent story; defensible.

**Phase B (later, if you want more):**

5. Displacement gate → upgrade, not new file.
6. IFVG (#7).
7. Breaker blocks (#6).

By that point HAL has 8 detectors, all Pine-mirrored, all composing.

---

## File layout after Tier 1

```
backend/ict/
  fvg.py
  order_blocks.py
  structure.py
  liquidity.py
  equal_levels.py        ← new (EQH/EQL)
  pd_zones.py            ← new (premium/discount + OTE)
  reference_levels.py    ← new (PDH/PDL/PWH/PWL)
  sessions.py            ← new (killzone tagging utility)
  detector.py            ← composition surface, gains 4 keys
```

`detector.detect_all()` output gains: `equal_highs`, `equal_lows`,
`pd_zones`, `reference_levels`. Killzones are stamped in-place onto
every existing event.

`pine/` gains `hal_eqh_eql.pine`, `hal_pd_zones.pine`,
`hal_reference_levels.pine`. Killzones don't need a Pine mirror — the
information lives on the timestamps of other events.

---

## Sources

- [Key ICT Concepts — Tradezella](https://www.tradezella.com/learning-items/key-ict-concepts)
- [ICT Abbreviations List — innercircletrader.net](https://innercircletrader.net/tutorials/ict-abbreviations/)
- [ICT Order Block — innercircletrader.net](https://innercircletrader.net/tutorials/ict-order-block/)
- [Order Blocks, Breaker Blocks, and Mitigation Blocks — theicttrader.com](https://theicttrader.com/2024/03/24/order-blocks-breaker-blocks-and-mitigation-blocks/)
- [ICT Mitigation Block — howtotrade.com](https://howtotrade.com/blog/mitigation-block-ict/)
- [Day 12: Breaker Blocks & Mitigation Blocks — tradingstrategyguides.com](https://tradingstrategyguides.com/day-12-breaker-blocks-mitigation-blocks-explained-ict-smc-deep-dive/)
- [ICT Optimal Trade Entry (OTE) — innercircletrader.net](https://innercircletrader.net/tutorials/ict-optimal-trade-entry-ote-pattern/)
- [ICT Premium and Discount — trinitytrading.io](https://blog.trinitytrading.io/ict-premium-discount-smart-money-guide-2026/)
- [ICT Dealing Range — fxnx.com](https://fxnx.com/en/blog/ict-dealing-range-map-institutional-moves)
- [Master ICT Kill Zones — innercircletrader.net](https://innercircletrader.net/tutorials/master-ict-kill-zones/)
- [ICT Killzones — tradingrage.com](https://tradingrage.com/learn/ict-killzone-explained)
- [ICT Kill Zones — howtotrade.com](https://howtotrade.com/blog/ict-kill-zones/)
