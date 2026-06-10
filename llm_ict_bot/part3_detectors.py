# %% [markdown]
# ## 6 · ICT/SMC detectors
#
# Deterministic, pure functions over an OHLCV DataFrame. They produce **context**
# records for the LLM, not trade signals. Definitions were researched fresh; each
# detector's docstring states the definition implemented and its source.
#
# | Concept | Definition implemented | Primary source |
# |---|---|---|
# | Fair Value Gap | 3-candle imbalance: bullish when `low[i] > high[i-2]` (gap = that span); candle 2 is the displacement candle | [innercircletrader.net — Valid ICT FVG](https://innercircletrader.net/tutorials/valid-ict-fair-value-gap/), [TrendSpider](https://trendspider.com/learning-center/fair-value-gap-trading-strategy/) |
# | Order Block | Last opposing candle before a displacement move that creates an FVG | [TradeZella — Key ICT Concepts](https://www.tradezella.com/learning-items/key-ict-concepts), [ePlanet](https://eplanetbrokers.com/training/what-is-fair-value-gap) |
# | Liquidity Sweep | Wick trades through a prior swing high/low, candle closes back inside | [Zeiierman](https://www.zeiierman.com/blog/liquidity-sweeps-in-trading), [Aron Groups — Liquidity in ICT](https://arongroups.co/technical-analyze/liquidity-in-ict/) |
# | BOS / CHoCH / MSS | BOS: close beyond a swing *with* the trend (continuation); CHoCH: close beyond a counter-trend swing (first reversal warning); HH/HL/LH/LL tracked | [innercircletrader.net — MSS](https://innercircletrader.net/tutorials/ict-market-structure-shift/), [FXOpen](https://fxopen.com/blog/en/market-structure-shift-meaning-and-use-in-ict-trading/), [TSG — BOS & CHoCH](https://tradingstrategyguides.com/day-3-smc-ict-market-structure-explained-bos-choch-swing-points-2026/) |
# | Liquidity Pool (EQH/EQL) | Consecutive swing highs/lows within a small tolerance — resting stop clusters | [Aron Groups](https://arongroups.co/technical-analyze/liquidity-in-ict/), [TradingFinder — Dealing Range](https://tradingfinder.com/education/forex/ict-dealing-range/) |
# | Premium / Discount | Equilibrium = 50% of the dealing range (swing low ↔ swing high); above = premium, below = discount | [TheSimpleICT](https://thesimpleict.com/premium-vs-discount-ict-smart-money/), [ICTFlow](https://ictflow.com/blog/ict-premium-discount-zones) |
# | Displacement | High-momentum candle: body ≫ recent average body — institutional participation | [TheSimpleICT — Dealing Range](https://thesimpleict.com/dealing-range-ict-guide/), [FXNX](https://fxnx.com/en/blog/ict-dealing-range-map-institutional-moves) |
#
# **Point-in-time discipline:** every record carries `confirmed_time` — the bar close
# at which the event became knowable. A swing with pivot strength `k` is only knowable
# `k` bars after its extreme prints; an FVG only when its third candle closes. The
# context builder filters on `confirmed_time <= decision_time`, and a prefix-consistency
# property test below proves no detector ever looks ahead.

# %%
# --- detector tunables -------------------------------------------------------------
SWING_K             = 2      # pivot strength: bars on each side that must be exceeded
DISPLACEMENT_FACTOR = 2.0    # candle body > factor × rolling mean body
DISPLACEMENT_WINDOW = 20     # rolling window for the mean body
EQ_TOL_PIPS         = 2.0    # equal-highs/lows tolerance
SWEEP_LOOKBACK_BARS = 500    # how long a swing level stays sweepable
OB_SCAN_BACK        = 10     # bars to scan back for the last opposing candle
SCAN_CAP_BARS       = 20_000 # forward-scan cap for FVG fill times


def _first_idx_after(arr: np.ndarray, start: int, threshold: float, op: str,
                     cap: int = SCAN_CAP_BARS) -> int:
    """First index j > start where `arr[j] <op> threshold`, scanning in chunks.
    Returns -1 if not found within `cap` bars. Pure index arithmetic — no pandas."""
    n = len(arr)
    j = start + 1
    end = min(n, start + 1 + cap)
    while j < end:
        hi = min(j + 256, end)
        chunk = arr[j:hi]
        mask = (chunk <= threshold) if op == "le" else (chunk >= threshold) \
            if op == "ge" else (chunk < threshold) if op == "lt" else (chunk > threshold)
        hit = int(np.argmax(mask))
        if mask[hit]:
            return j + hit
        j = hi
    return -1


def detect_swings(df: pd.DataFrame, k: int = SWING_K) -> pd.DataFrame:
    """Swing (fractal) pivots: a swing high at bar i has a high strictly greater than
    the highs of the k bars on each side; mirror for swing lows. The pivot is only
    *knowable* once the k-th later bar closes → `confirmed_time = close_time[i+k]`.
    Swings are the substrate for sweeps, structure, pools and the dealing range.
    (Standard SMC swing-point definition — see TSG Day-3 market-structure guide.)"""
    h, l = df["high"].values, df["low"].values
    n = len(df)
    sh = np.ones(n, bool)
    sl = np.ones(n, bool)
    for j in range(1, k + 1):
        sh[:j], sh[n - j:] = False, False
        sl[:j], sl[n - j:] = False, False
        sh[j:n] &= h[j:n] > h[:n - j]          # vs j bars before
        sh[:n - j] &= h[:n - j] > h[j:n]       # vs j bars after
        sl[j:n] &= l[j:n] < l[:n - j]
        sl[:n - j] &= l[:n - j] < l[j:n]
    rows = []
    ct = df["close_time"].values
    idx = df.index
    for i in np.flatnonzero(sh | sl):
        if i + k >= n:
            continue                            # not yet confirmable inside this df
        if sh[i]:
            rows.append({"time": idx[i], "kind": "high", "level": h[i],
                         "confirmed_time": ct[i + k]})
        if sl[i]:
            rows.append({"time": idx[i], "kind": "low", "level": l[i],
                         "confirmed_time": ct[i + k]})
    cols = ["time", "kind", "level", "confirmed_time"]
    out = pd.DataFrame(rows, columns=cols).sort_values(
        ["confirmed_time", "time", "kind"], kind="mergesort")   # stable: ties deterministic
    return out.reset_index(drop=True)


def detect_fvg(df: pd.DataFrame) -> pd.DataFrame:
    """Fair Value Gaps — the ICT 3-candle imbalance.
    Bullish FVG: `low[i] > high[i-2]` → untraded span [high[i-2], low[i]]; candle i-1
    is the displacement candle. Bearish mirror: `high[i] < low[i-2]`.
    Knowable when candle i closes → `confirmed_time = close_time[i]`.
    Sources: innercircletrader.net 'Valid ICT Fair Value Gap', TrendSpider FVG guide.

    `touch_time`/`fill_time` (first re-entry into the gap / first full fill) are
    computed over the full series ONCE for speed. They are leakage-safe because the
    context builder only ever *compares* them against the decision time t — i.e. it
    reveals exactly 'is this gap still open as of t?', nothing about the future."""
    h, l = df["high"].values, df["low"].values
    n = len(df)
    ct, idx = df["close_time"].values, df.index
    rows = []
    bull = np.flatnonzero(l[2:] > h[:-2]) + 2
    bear = np.flatnonzero(h[2:] < l[:-2]) + 2
    for i in bull:
        bottom, top = h[i - 2], l[i]
        ft = _first_idx_after(l, i, top, "lt")     # price re-enters gap from above
        ff = _first_idx_after(l, i, bottom, "le")  # full fill: traded to far edge
        rows.append({"time": idx[i], "direction": "bullish", "top": top, "bottom": bottom,
                     "confirmed_time": ct[i],
                     "touch_time": ct[ft] if ft >= 0 else pd.NaT,    # close-time of the
                     "fill_time": ct[ff] if ff >= 0 else pd.NaT})    # touching/filling bar
    for i in bear:
        bottom, top = h[i], l[i - 2]
        ft = _first_idx_after(h, i, bottom, "gt")
        ff = _first_idx_after(h, i, top, "ge")
        rows.append({"time": idx[i], "direction": "bearish", "top": top, "bottom": bottom,
                     "confirmed_time": ct[i],
                     "touch_time": ct[ft] if ft >= 0 else pd.NaT,
                     "fill_time": ct[ff] if ff >= 0 else pd.NaT})
    cols = ["time", "direction", "top", "bottom", "confirmed_time", "touch_time", "fill_time"]
    return (pd.DataFrame(rows, columns=cols)
            .sort_values(["time", "direction"], kind="mergesort").reset_index(drop=True))


def detect_displacement(df: pd.DataFrame, factor: float = DISPLACEMENT_FACTOR,
                        window: int = DISPLACEMENT_WINDOW) -> pd.DataFrame:
    """Displacement — a high-momentum, large-bodied candle signalling institutional
    participation (TheSimpleICT / FXNX dealing-range guides). Implemented as:
    candle body > `factor` × rolling mean body of the prior `window` candles.
    Knowable at its own close."""
    body = (df["close"] - df["open"]).abs()
    avg = body.rolling(window, min_periods=5).mean().shift(1)
    mask = (body > factor * avg).to_numpy()
    out = pd.DataFrame({
        "time": df.index[mask],
        "direction": np.where(df["close"].to_numpy()[mask] >= df["open"].to_numpy()[mask],
                              "bullish", "bearish"),
        "body": body.to_numpy()[mask],
        "confirmed_time": df["close_time"].to_numpy()[mask],
    })
    return out.reset_index(drop=True)


def detect_order_blocks(df: pd.DataFrame, fvg: Optional[pd.DataFrame] = None,
                        displacement: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """Order Blocks — 'the last opposing candle before a displacement move; the base
    of an impulsive move where large orders entered' (TradeZella key-ICT-concepts;
    ePlanet OB guide). Implemented strictly: a bullish OB is the last down-close
    candle within `OB_SCAN_BACK` bars before an up-displacement candle that creates
    a bullish FVG (the FVG requirement is the imbalance confirmation the sources
    demand). Zone = the opposing candle's full range. Knowable when the FVG's third
    candle closes (that is when the displacement is proven)."""
    fvg = detect_fvg(df) if fvg is None else fvg
    disp = detect_displacement(df) if displacement is None else displacement
    disp_times = set(disp["time"])
    o, c, h, l = df["open"].values, df["close"].values, df["high"].values, df["low"].values
    pos = {t: i for i, t in enumerate(df.index)}
    rows, seen = [], set()
    for r in fvg.itertuples():
        i = pos[r.time]                      # third candle of the FVG
        mid = i - 1                          # displacement candle
        if mid < 0 or df.index[mid] not in disp_times:
            continue
        want_down = r.direction == "bullish"   # bullish OB = last down-close candle
        for j in range(mid - 1, max(-1, mid - 1 - OB_SCAN_BACK), -1):
            opposing = (c[j] < o[j]) if want_down else (c[j] > o[j])
            if opposing:
                if df.index[j] in seen:
                    break
                seen.add(df.index[j])
                rows.append({"time": df.index[j],
                             "direction": "bullish" if want_down else "bearish",
                             "top": h[j], "bottom": l[j],
                             "confirmed_time": r.confirmed_time})
                break
    cols = ["time", "direction", "top", "bottom", "confirmed_time"]
    return (pd.DataFrame(rows, columns=cols)
            .sort_values(["time", "direction"], kind="mergesort").reset_index(drop=True))


def detect_liquidity_sweeps(df: pd.DataFrame, swings: Optional[pd.DataFrame] = None,
                            lookback: int = SWEEP_LOOKBACK_BARS) -> pd.DataFrame:
    """Liquidity Sweeps — price wicks through a prior swing high/low (where resting
    stops sit), then the candle closes back inside the range: a stop raid, not a
    genuine break (Zeiierman liquidity-sweep guide; Aron Groups 'Liquidity in ICT').
    Sweeping a swing HIGH grabs buy-side liquidity (bearish implication); sweeping a
    swing LOW grabs sell-side liquidity (bullish implication).
    For each confirmed swing, the FIRST later candle to trade through the level
    decides: close back inside → sweep; close beyond → genuine break, no record.
    Knowable at that candle's close."""
    swings = detect_swings(df) if swings is None else swings
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    ct, idx = df["close_time"].values, df.index
    pos = {t: i for i, t in enumerate(idx)}
    rows = []
    for s in swings.itertuples():
        i = pos[s.time]
        start = i + SWING_K                  # search begins after the pivot confirms
        if s.kind == "high":
            j = _first_idx_after(h, start, s.level, "gt", cap=lookback)
            if j >= 0 and c[j] < s.level:
                rows.append({"time": idx[j], "side": "buyside", "swept_level": s.level,
                             "swing_time": s.time, "wick_extreme": h[j],
                             "confirmed_time": ct[j]})
        else:
            j = _first_idx_after(l, start, s.level, "lt", cap=lookback)
            if j >= 0 and c[j] > s.level:
                rows.append({"time": idx[j], "side": "sellside", "swept_level": s.level,
                             "swing_time": s.time, "wick_extreme": l[j],
                             "confirmed_time": ct[j]})
    cols = ["time", "side", "swept_level", "swing_time", "wick_extreme", "confirmed_time"]
    out = pd.DataFrame(rows, columns=cols).sort_values(
        ["time", "swing_time", "swept_level"], kind="mergesort")  # one candle can sweep
    return out.reset_index(drop=True)                             # several levels at once


def detect_structure(df: pd.DataFrame, swings: Optional[pd.DataFrame] = None
                     ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Market structure — BOS / CHoCH events + HH/HL/LH/LL swing labels.
    BOS (Break of Structure): a candle CLOSE beyond the latest confirmed swing in the
    direction of the prevailing trend → continuation. CHoCH (Change of Character): a
    close beyond the latest counter-trend swing → first reversal warning. The first
    structural break of a run (no trend yet) is labelled CHoCH, matching the
    'change of character starts a new regime' reading.
    Sources: innercircletrader.net MSS guide; FXOpen MSS; TSG Day-3 (BOS continuation
    vs CHoCH reversal; swing labels HH/HL/LH/LL).
    Returns (events, labelled_swings); events are knowable at the breaking close."""
    swings = detect_swings(df) if swings is None else swings
    c = df["close"].values
    ct, idx = df["close_time"].values, df.index
    pos = {t: i for i, t in enumerate(idx)}
    confirm_at: dict[int, list] = {}
    for s in swings.itertuples():
        confirm_at.setdefault(pos[s.time] + SWING_K, []).append(s)
    trend = 0                                # 0 none, +1 up, -1 down
    ref_high = ref_low = None                # latest confirmed, not-yet-broken swings
    last_high_lvl = last_low_lvl = None
    labels, events = [], []
    for i in range(len(df)):
        for s in confirm_at.get(i, []):
            if s.kind == "high":
                lab = None if last_high_lvl is None else ("HH" if s.level > last_high_lvl else "LH")
                last_high_lvl, ref_high = s.level, s
            else:
                lab = None if last_low_lvl is None else ("HL" if s.level > last_low_lvl else "LL")
                last_low_lvl, ref_low = s.level, s
            labels.append({"time": s.time, "kind": s.kind, "level": s.level,
                           "label": lab, "confirmed_time": s.confirmed_time})
        if ref_high is not None and c[i] > ref_high.level:
            kind = "BOS_up" if trend == 1 else "CHoCH_up"
            trend = 1
            events.append({"time": idx[i], "kind": kind, "level": ref_high.level,
                           "trend_after": trend, "confirmed_time": ct[i]})
            ref_high = None
        elif ref_low is not None and c[i] < ref_low.level:
            kind = "BOS_down" if trend == -1 else "CHoCH_down"
            trend = -1
            events.append({"time": idx[i], "kind": kind, "level": ref_low.level,
                           "trend_after": trend, "confirmed_time": ct[i]})
            ref_low = None
    ev_cols = ["time", "kind", "level", "trend_after", "confirmed_time"]
    lb_cols = ["time", "kind", "level", "label", "confirmed_time"]
    return (pd.DataFrame(events, columns=ev_cols),
            pd.DataFrame(labels, columns=lb_cols))


def detect_equal_levels(df: pd.DataFrame, swings: Optional[pd.DataFrame] = None,
                        tol_pips: float = EQ_TOL_PIPS) -> pd.DataFrame:
    """Liquidity Pools — equal highs (EQH) / equal lows (EQL): consecutive swing
    highs/lows within `tol_pips`, marking clustered resting stops (buy-side liquidity
    above EQH, sell-side below EQL). Sources: Aron Groups 'Liquidity in ICT';
    TradingFinder dealing-range liquidity guide.
    Knowable when the second swing of the pair confirms."""
    swings = detect_swings(df) if swings is None else swings
    rows = []
    for kind, pool, side in (("high", "EQH", "buyside"), ("low", "EQL", "sellside")):
        sub = swings[swings["kind"] == kind].sort_values("time")
        prev = None
        for s in sub.itertuples():
            if prev is not None and abs(s.level - prev.level) <= tol_pips * PIP:
                rows.append({"time": s.time, "pool": pool, "side": side,
                             "level": max(s.level, prev.level) if kind == "high"
                                      else min(s.level, prev.level),
                             "first_time": prev.time, "confirmed_time": s.confirmed_time})
            prev = s
    cols = ["time", "pool", "side", "level", "first_time", "confirmed_time"]
    return (pd.DataFrame(rows, columns=cols)
            .sort_values(["time", "pool", "level"], kind="mergesort").reset_index(drop=True))


def premium_discount(visible_swings: pd.DataFrame, price: float,
                     eq_band: float = 0.05) -> Optional[dict]:
    """Premium / Discount — the dealing range runs from the most recent confirmed
    swing low to the most recent confirmed swing high; equilibrium is its 50% level.
    Price above EQ trades at a premium (favour shorts / take profits), below EQ at a
    discount (favour longs). Sources: TheSimpleICT premium-vs-discount; ICTFlow
    premium-discount zones. Pure function of already-visible swings → inherently
    point-in-time."""
    highs = visible_swings[visible_swings["kind"] == "high"]
    lows = visible_swings[visible_swings["kind"] == "low"]
    if highs.empty or lows.empty:
        return None
    hi = highs.iloc[-1]["level"]
    lo = lows.iloc[-1]["level"]
    if hi <= lo:
        return None
    pos = (price - lo) / (hi - lo)
    zone = "premium" if pos > 0.5 + eq_band else "discount" if pos < 0.5 - eq_band else "equilibrium"
    return {"range_high": hi, "range_low": lo, "equilibrium": (hi + lo) / 2,
            "position": pos, "zone": zone}


def run_all_detectors(df: pd.DataFrame) -> dict:
    """Run every detector once over a (full-history) TF frame. The context builder
    later filters each record set by `confirmed_time <= decision_time`."""
    swings = detect_swings(df)
    fvg = detect_fvg(df)
    disp = detect_displacement(df)
    events, labelled = detect_structure(df, swings)
    return {"swings": labelled, "fvg": fvg, "displacement": disp,
            "order_blocks": detect_order_blocks(df, fvg, disp),
            "sweeps": detect_liquidity_sweeps(df, swings),
            "structure": events,
            "pools": detect_equal_levels(df, swings)}

print("detectors defined")
