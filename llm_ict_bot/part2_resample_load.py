# %% [markdown]
# ## 3 · Resampling — closed candles only
#
# All higher timeframes are derived from the cleaned 1m series with pandas
# `resample()` (open=first, high=max, low=min, close=last, volume=sum).
#
# **The `close_time` convention (load-bearing for leakage safety):** every resampled
# bar carries `close_time = bar_start + timeframe`. A bar is *knowable* at decision
# time `t` iff `close_time <= t`. The context builder filters on exactly that, so a
# partially-formed current bar can never appear in any context.
#
# Daily bars are anchored at **21:00 UTC** (≈ 5 pm New York, the FX day boundary), so
# Sunday-evening trading folds into Monday's daily bar instead of forming stub bars.
# The anchor is fixed year-round; in winter the true 5 pm-ET boundary is 22:00 UTC, so
# daily bars are offset by 1 h for part of the year — a documented simplification that
# affects only daily-bar aesthetics, never intraday logic.

# %%
TF_DELTA = {
    "1min": pd.Timedelta("1min"), "5min": pd.Timedelta("5min"),
    "15min": pd.Timedelta("15min"), "1h": pd.Timedelta("1h"),
    "4h": pd.Timedelta("4h"), "1d": pd.Timedelta("24h"),
}

def resample_ohlcv(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Resample cleaned 1m bars to `rule`. Bars are labelled by start time and carry
    an explicit `close_time`; bars with no underlying 1m data are dropped (gaps are
    never filled)."""
    if rule == "1min":
        out = df_1m.copy()
    else:
        resampler = (df_1m.resample("24h", offset=FX_DAY_OFFSET) if rule == "1d"
                     else df_1m.resample(rule))
        out = resampler.agg(open=("open", "first"), high=("high", "max"),
                            low=("low", "min"), close=("close", "last"),
                            volume=("volume", "sum")).dropna(subset=["open"])
    out["close_time"] = out.index + TF_DELTA[rule]
    return out


def build_tf_store(df_1m: pd.DataFrame, tfs: Iterable[str]) -> dict[str, pd.DataFrame]:
    """Resample the cleaned 1m base once per context timeframe."""
    return {tf: resample_ohlcv(df_1m, tf) for tf in tfs}

# %% [markdown]
# ## 4 · Synthetic fixture data
#
# A deterministic synthetic 1m EUR/USD series (seeded random walk over weekdays).
# It powers the detector unit tests' property checks and the end-to-end smoke test,
# so the notebook runs even before the real Parquet store is wired in.

# %%
def make_synthetic_1m(start: str = "2024-01-01", days: int = 20,
                      seed: int = RNG_SEED, p0: float = 1.0850) -> pd.DataFrame:
    """Seeded 1m random-walk OHLCV over `days` weekdays, 24h/day, tz-naive UTC."""
    rng = np.random.default_rng(seed)
    bdays = pd.bdate_range(start, periods=days)
    idx = pd.DatetimeIndex(
        np.concatenate([pd.date_range(d, periods=1440, freq="1min").values for d in bdays])
    )
    n = len(idx)
    # mid-price walk with mild vol clustering
    vol = 0.00006 * (1 + 0.5 * np.sin(np.arange(n) / 700))
    steps = rng.normal(0, 1, n) * vol
    mid = p0 + np.cumsum(steps)
    opens = np.r_[p0, mid[:-1]]
    closes = mid
    wick = np.abs(rng.normal(0, 1, (2, n))) * vol
    highs = np.maximum(opens, closes) + wick[0]
    lows = np.minimum(opens, closes) - wick[1]
    volume = rng.lognormal(3, 1, n).round(2)
    df = pd.DataFrame({"open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": volume}, index=idx)
    return df.round(5)

print("synthetic generator ready:", make_synthetic_1m(days=2).shape)

# %% [markdown]
# ## 5 · Load & clean the real store
#
# Loads the canonical 1m Parquet if present, otherwise falls back to synthetic data
# (`HAVE_REAL_DATA` records which). Cleaning results — weekend bars dropped, holidays
# dropped, gaps and thin days flagged — are printed and kept for the report.

# %%
HAVE_REAL_DATA = PARQUET_1M.exists()

raw_1m = load_1m(PARQUET_1M) if HAVE_REAL_DATA else make_synthetic_1m(days=40)
clean_1m, n_weekend = drop_weekends(raw_1m)
clean_1m, holiday_days = drop_holidays(clean_1m)
gaps_df = flag_gaps(clean_1m)
thin_days = flag_thin_days(clean_1m)

print(f"source            : {'real parquet' if HAVE_REAL_DATA else 'SYNTHETIC fallback'}")
print(f"raw 1m bars       : {len(raw_1m):,}")
print(f"weekend bars cut  : {n_weekend:,}")
print(f"holiday days cut  : {len(holiday_days)}")
print(f"clean 1m bars     : {len(clean_1m):,}  ({clean_1m.index[0]} → {clean_1m.index[-1]})")
print(f"intra-session gaps: {len(gaps_df):,} flagged (largest: "
      f"{gaps_df['minutes'].max():.0f} min)" if len(gaps_df) else "intra-session gaps: none")
print(f"thin days flagged : {len(thin_days)} (logged, not dropped)")

TF_STORE = build_tf_store(clean_1m, CONTEXT_TFS)
for tf, d in TF_STORE.items():
    print(f"  {tf:>5}: {len(d):,} bars")
