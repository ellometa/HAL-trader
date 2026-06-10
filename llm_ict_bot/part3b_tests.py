# %% [markdown]
# ## 7 · Detector unit tests
#
# Two layers:
#
# 1. **Hand-crafted fixtures** — tiny bar sequences where the expected detections are
#    known exactly (levels, directions, confirmation times).
# 2. **Prefix-consistency property test** — for any cut point `t`, running a detector
#    on `df[:t]` must yield *exactly* the records the full run produces with
#    `confirmed_time <= t`. If a detector peeked at future bars, prefix and full runs
#    would disagree. This is the machine-checked no-lookahead guarantee.

# %%
def bars(rows, start="2024-01-06", freq="15min") -> pd.DataFrame:
    """Fixture builder: rows of (open, high, low, close) → OHLCV frame w/ close_time."""
    idx = pd.date_range(start, periods=len(rows), freq=freq)
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    df["close_time"] = df.index + pd.Timedelta(freq)
    return df


# --- FVG: bullish gap, touch, full fill -------------------------------------------
fx = bars([
    (1.0000, 1.0010, 0.9990, 1.0005),   # c0
    (1.0005, 1.0040, 1.0004, 1.0038),   # c1 displacement
    (1.0038, 1.0050, 1.0020, 1.0045),   # c2 → bullish FVG [1.0010, 1.0020]
    (1.0045, 1.0048, 1.0015, 1.0046),   # c3 dips into gap (touch, not full fill)
    (1.0046, 1.0047, 1.0005, 1.0040),   # c4 trades to far edge (full fill)
])
f = detect_fvg(fx)
assert len(f) == 1 and f.iloc[0]["direction"] == "bullish"
assert math.isclose(f.iloc[0]["bottom"], 1.0010) and math.isclose(f.iloc[0]["top"], 1.0020)
assert f.iloc[0]["confirmed_time"] == fx["close_time"].iloc[2]
assert f.iloc[0]["touch_time"] == fx["close_time"].iloc[3]   # knowable at that bar's close
assert f.iloc[0]["fill_time"] == fx["close_time"].iloc[4]

# bearish mirror
fx2 = bars([
    (1.0050, 1.0060, 1.0040, 1.0045),
    (1.0045, 1.0046, 1.0010, 1.0012),
    (1.0012, 1.0030, 1.0005, 1.0008),   # high 1.0030 < low c0 1.0040 → bearish FVG
])
f2 = detect_fvg(fx2)
assert len(f2) == 1 and f2.iloc[0]["direction"] == "bearish"
assert math.isclose(f2.iloc[0]["bottom"], 1.0030) and math.isclose(f2.iloc[0]["top"], 1.0040)

# --- swings: pivot strength & confirmation lag -------------------------------------
sx = bars([
    (1.0010, 1.0015, 1.0005, 1.0012),
    (1.0012, 1.0020, 1.0008, 1.0018),
    (1.0018, 1.0050, 1.0016, 1.0045),   # swing high @2 (k=2)
    (1.0045, 1.0046, 1.0014, 1.0020),
    (1.0020, 1.0025, 1.0012, 1.0022),
    (1.0022, 1.0028, 1.0015, 1.0024),
])
sw = detect_swings(sx)
sw_h = sw[sw["kind"] == "high"]
assert len(sw_h) == 1 and math.isclose(sw_h.iloc[0]["level"], 1.0050)
assert sw_h.iloc[0]["time"] == sx.index[2]
assert sw_h.iloc[0]["confirmed_time"] == sx["close_time"].iloc[4]   # knowable k bars later

# --- liquidity sweep: wick through swing high, close back inside -------------------
lx = bars([
    (1.0010, 1.0018, 1.0005, 1.0012),
    (1.0012, 1.0020, 1.0008, 1.0015),
    (1.0015, 1.0030, 1.0012, 1.0022),   # swing high @2, level 1.0030
    (1.0022, 1.0024, 1.0010, 1.0014),
    (1.0014, 1.0022, 1.0008, 1.0016),   # swing confirmed at close of @4
    (1.0016, 1.0035, 1.0012, 1.0020),   # wick 1.0035 > 1.0030, close 1.0020 < 1.0030 → sweep
])
sweeps = detect_liquidity_sweeps(lx)
assert len(sweeps) == 1
s0 = sweeps.iloc[0]
assert s0["side"] == "buyside" and math.isclose(s0["swept_level"], 1.0030)
assert s0["time"] == lx.index[5] and math.isclose(s0["wick_extreme"], 1.0035)

# negative control: a close *above* the level is a genuine break, not a sweep
lx_break = lx.copy()
lx_break.loc[lx_break.index[5], "close"] = 1.0032   # still under the 1.0035 wick high
assert len(detect_liquidity_sweeps(lx_break)) == 0

# --- market structure: CHoCH → HH/HL labels → BOS ----------------------------------
mx = bars([
    (1.0020, 1.0030, 1.0010, 1.0022),
    (1.0022, 1.0028, 1.0008, 1.0018),
    (1.0018, 1.0020, 1.0000, 1.0012),   # swing low @2 (1.0000)
    (1.0012, 1.0022, 1.0005, 1.0018),
    (1.0018, 1.0024, 1.0006, 1.0020),
    (1.0020, 1.0040, 1.0015, 1.0030),   # swing high @5 (1.0040)
    (1.0030, 1.0030, 1.0012, 1.0016),
    (1.0016, 1.0028, 1.0010, 1.0015),   # swing low @7 (1.0010) → HL
    (1.0015, 1.0045, 1.0013, 1.0044),   # close 1.0044 > 1.0040 → CHoCH_up (first break)
    (1.0044, 1.0050, 1.0016, 1.0042),   # swing high @9 (1.0050) → HH
    (1.0038, 1.0040, 1.0020, 1.0030),
    (1.0030, 1.0038, 1.0018, 1.0028),
    (1.0028, 1.0060, 1.0025, 1.0058),   # close 1.0058 > 1.0050 → BOS_up (trend continues)
    (1.0050, 1.0055, 1.0030, 1.0040),
    (1.0040, 1.0050, 1.0032, 1.0042),
])
events, labelled = detect_structure(mx)
assert list(events["kind"]) == ["CHoCH_up", "BOS_up"], list(events["kind"])
assert events.iloc[0]["time"] == mx.index[8] and math.isclose(events.iloc[0]["level"], 1.0040)
assert events.iloc[1]["time"] == mx.index[12] and math.isclose(events.iloc[1]["level"], 1.0050)
lab = {(r["time"], r["label"]) for _, r in labelled.iterrows() if r["label"]}
assert (mx.index[7], "HL") in lab and (mx.index[9], "HH") in lab

# --- order block: last down candle before FVG-creating displacement ----------------
small = [(1.0000 + i * 0.0002, 1.0006 + i * 0.0002, 0.9998 + i * 0.0002, 1.0005 + i * 0.0002)
         for i in range(8)]                                   # 8 small bars (~5-pip bodies)
ox = bars(small + [
    (1.0020, 1.0022, 1.0008, 1.0010),   # @8 down candle — the order block
    (1.0010, 1.0052, 1.0009, 1.0050),   # @9 up displacement (~40-pip body)
    (1.0050, 1.0060, 1.0048, 1.0058),   # @10 low 1.0048 > high@8 1.0022 → bullish FVG
])
obs = detect_order_blocks(ox)
assert len(obs) == 1
ob = obs.iloc[0]
assert ob["direction"] == "bullish" and ob["time"] == ox.index[8]
assert math.isclose(ob["top"], 1.0022) and math.isclose(ob["bottom"], 1.0008)
assert ob["confirmed_time"] == ox["close_time"].iloc[10]      # knowable only after the FVG proves it
assert len(detect_displacement(ox)) >= 1                      # @9 qualifies on its own

# --- liquidity pool: equal highs within tolerance ----------------------------------
ex = bars([
    (1.0008, 1.0010, 1.0000, 1.0006),
    (1.0006, 1.0015, 1.0002, 1.0010),
    (1.0010, 1.0030, 1.0006, 1.0020),   # swing high @2 (1.0030)
    (1.0014, 1.0016, 1.0004, 1.0008),
    (1.0008, 1.0010, 1.0002, 1.0006),
    (1.0006, 1.0014, 1.0001, 1.0010),
    (1.0010, 1.0031, 1.0005, 1.0018),   # swing high @6 (1.0031) — 1 pip apart → EQH
    (1.0010, 1.0012, 1.0003, 1.0008),
    (1.0008, 1.0010, 1.0001, 1.0006),
])
pools = detect_equal_levels(ex)
eqh = pools[pools["pool"] == "EQH"]
assert len(eqh) == 1 and math.isclose(eqh.iloc[0]["level"], 1.0031)
assert eqh.iloc[0]["side"] == "buyside"

# --- premium/discount: 50% equilibrium of the dealing range ------------------------
vis = pd.DataFrame({"time": pd.date_range("2024-01-06", periods=2, freq="1h"),
                    "kind": ["low", "high"], "level": [1.0000, 1.0100],
                    "confirmed_time": pd.date_range("2024-01-06 02:00", periods=2, freq="1h")})
pdd = premium_discount(vis, price=1.0080)
assert pdd["zone"] == "premium" and math.isclose(pdd["equilibrium"], 1.0050)
assert premium_discount(vis, price=1.0020)["zone"] == "discount"
assert premium_discount(vis, price=1.0052)["zone"] == "equilibrium"

print("hand-crafted detector fixtures: all assertions passed")

# %% [markdown]
# ### Prefix-consistency property test (no-lookahead proof)
#
# For each detector and a set of cut points over a synthetic series: records produced
# from the truncated frame must equal the full-run records filtered to
# `confirmed_time <= cut`. FVG `touch_time`/`fill_time` are excluded from the
# comparison — they legitimately summarize later bars and are only ever *compared
# against* the decision time downstream (never displayed as future knowledge).

# %%
def assert_prefix_consistent(name: str, fn: Callable[[pd.DataFrame], pd.DataFrame],
                             df: pd.DataFrame, n_cuts: int = 6,
                             drop_cols: tuple = ("touch_time", "fill_time")) -> None:
    full = fn(df)
    cuts = np.linspace(60, len(df) - 1, n_cuts, dtype=int)
    for cut in cuts:
        prefix = df.iloc[:cut]
        t = prefix["close_time"].iloc[-1]
        got = fn(prefix).drop(columns=list(drop_cols), errors="ignore").reset_index(drop=True)
        want = (full[full["confirmed_time"] <= t]
                .drop(columns=list(drop_cols), errors="ignore").reset_index(drop=True))
        pd.testing.assert_frame_equal(got, want, check_dtype=False, check_exact=False,
                                      obj=f"{name} @cut={cut}")


_syn15 = resample_ohlcv(drop_weekends(make_synthetic_1m(days=15, seed=7))[0], "15min")
assert_prefix_consistent("swings", detect_swings, _syn15)
assert_prefix_consistent("fvg", detect_fvg, _syn15)
assert_prefix_consistent("displacement", detect_displacement, _syn15)
assert_prefix_consistent("order_blocks", detect_order_blocks, _syn15)
assert_prefix_consistent("sweeps", detect_liquidity_sweeps, _syn15)
assert_prefix_consistent("structure_events", lambda d: detect_structure(d)[0], _syn15)
assert_prefix_consistent("structure_labels", lambda d: detect_structure(d)[1], _syn15)
assert_prefix_consistent("pools", detect_equal_levels, _syn15)

print(f"prefix-consistency: all 8 detectors leakage-clean over {len(_syn15):,} synthetic 15m bars")
