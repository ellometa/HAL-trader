#!/usr/bin/env python3
"""
xauusd.py — Re-runnable XAU/USD historical data pipeline for ICT/SMC backtesting.

Source: Dukascopy tick (XAU/USD, 2003-05 onward), fetched + decoded directly from
the public .bi5 datafeed (no API key, no account, pure-stdlib LZMA). We do NOT use
the `dukascopy-python` lib: it silently returns empty for XAU/USD before ~mid-2012,
which would leave an 8-year hole. Dukascopy is UTC-native (no EST->UTC trap); HistData
(1m, EST without DST) remains the documented fallback — see README.

Layers
  1. data/xauusd_tick/year=YYYY/YYYY-MM-DD.parquet  canonical raw tick (untouched)
  2. data/xauusd_1m.parquet                          1m base, aggregated from tick
  3. data/xauusd_{5m,15m,30m,1h,4h,1d}.parquet       resampled from the 1m base

Conventions
  * All timestamps UTC, tz-naive (datetime64[ns]).
  * Bar files:  Datetime, Open, High, Low, Close, Volume.
  * Tick files: Datetime, Bid, Ask, BidVol, AskVol.
  * Price basis for bars = MID = (Bid+Ask)/2. Bar Volume = sum(BidVol+AskVol).
  * Bar boundary: resample(closed='left', label='left') -> timestamp is the bar OPEN.
  * Higher TFs are ALWAYS resampled from the 1m base, never chained.

Commands:  fetch | build1m | resample | report | all
Run `python xauusd.py all --help` for flags (--start/--end/--sample/--force).
"""
from __future__ import annotations

import argparse
import json
import lzma
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.dataset as pads
import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR / "data"
TICK_DIR = DATA_DIR / "xauusd_tick"
ONE_MIN_PATH = DATA_DIR / "xauusd_1m.parquet"
MANIFEST_PATH = DATA_DIR / "MANIFEST.json"
REPORT_PATH = DATA_DIR / "REPORT.md"

# Dukascopy XAU/USD tick exists from ~2003-05-04, but pre-2013 files are "cold" on
# their servers (rarely requested -> not CDN-cached -> throttled to ~5 req/s, and high
# concurrency just gets rejected), so a 2003-2012 backfill takes ~3-4h. Recent years
# are CDN-fast. We default to 2013 for a clean, quick, gap-free dataset; pass
# --start 2003-05-04 if you want the full (slow) history.
DUKA_START = datetime(2003, 5, 4, tzinfo=timezone.utc)
DEFAULT_START = datetime(2013, 1, 1, tzinfo=timezone.utc)

# Higher timeframes: name -> (output path stem, pandas offset alias).
# Modern aliases (pandas >=2.2): 'min'/'h'/'D' (not 'T'/'H').
TIMEFRAMES = {
    "5m":  ("xauusd_5m",  "5min"),
    "15m": ("xauusd_15m", "15min"),
    "30m": ("xauusd_30m", "30min"),
    "1h":  ("xauusd_1h",  "1h"),
    "4h":  ("xauusd_4h",  "4h"),
    "1d":  ("xauusd_1d",  "1D"),
}
BAR_AGG = {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}

TICK_SCHEMA = ["Datetime", "Bid", "Ask", "BidVol", "AskVol"]
BAR_SCHEMA = ["Datetime", "Open", "High", "Low", "Close", "Volume"]


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def write_parquet_atomic(df: pd.DataFrame, path: Path) -> None:
    """Write to a temp file then os.replace — an interrupted run never leaves a
    half-written parquet that the skip-if-exists logic would later trust."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", compression="snappy", index=False)
    os.replace(tmp, path)


def parquet_ok(path: Path) -> bool:
    """A previously written chunk is reusable only if it reads back cleanly."""
    if not path.exists():
        return False
    try:
        pd.read_parquet(path, columns=[TICK_SCHEMA[0]])
        return True
    except Exception:
        return False


def empty_tick_frame() -> pd.DataFrame:
    return pd.DataFrame({
        "Datetime": pd.Series([], dtype="datetime64[ns]"),
        "Bid": pd.Series([], dtype="float64"),
        "Ask": pd.Series([], dtype="float64"),
        "BidVol": pd.Series([], dtype="float64"),
        "AskVol": pd.Series([], dtype="float64"),
    })


def day_range(start: datetime, end: datetime):
    """Yield UTC midnight datetimes for each day in [start, end)."""
    d = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end = datetime(end.year, end.month, end.day, tzinfo=timezone.utc)
    while d < end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------- #
# Step 1 — fetch raw tick directly from Dukascopy's .bi5 datafeed
# --------------------------------------------------------------------------- #
# We talk to the raw datafeed rather than the `dukascopy-python` lib: that lib
# SILENTLY returns empty for XAU/USD before ~mid-2012 (a real bug — the .bi5 files
# exist and decode fine), which would leave an ~8-year hole. Decoding .bi5 ourselves
# is byte-identical to the lib on the years it does handle, and pure-stdlib.
#
# .bi5 = LZMA-compressed array of 20-byte big-endian records:
#   int32 ms-from-hour | int32 ask | int32 bid | float32 askVol | float32 bidVol
# Prices are integers scaled by the instrument point factor (XAU/USD: 1e5).
# URL months are ZERO-indexed (Jan=00 ... Dec=11).
DUKA_URL = "https://datafeed.dukascopy.com/datafeed/XAUUSD/{y}/{mi:02d}/{d:02d}/{h:02d}h_ticks.bi5"
POINT = 1e3  # XAU/USD quotes carry 3 decimals (Dukascopy point factor)
_BI5_DTYPE = np.dtype([("ms", ">i4"), ("ask", ">i4"), ("bid", ">i4"),
                       ("av", ">f4"), ("bv", ">f4")])
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "xauusd-fetch/1.0"})
# Large connection pool so many threads reuse keep-alive connections to the single
# host instead of re-doing TLS per request (the default pool_maxsize=10 throttles us).
_SESSION.mount("https://", requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64))
HOUR_WORKERS = 12  # concurrent hourly .bi5 GETs within one day (24 files/day is latency-bound)


def _decode_bi5(content: bytes, hour_start: pd.Timestamp):
    """Decode one hourly .bi5 blob -> tick frame (tz-naive UTC). None if no ticks."""
    if not content:
        return None
    data = lzma.LZMADecompressor(format=lzma.FORMAT_AUTO).decompress(content)
    rec = np.frombuffer(data, dtype=_BI5_DTYPE)
    if rec.size == 0:
        return None
    return pd.DataFrame({
        "Datetime": hour_start + pd.to_timedelta(rec["ms"].astype("int64"), unit="ms"),
        "Bid": rec["bid"] / POINT,
        "Ask": rec["ask"] / POINT,
        "BidVol": rec["bv"].astype("float64"),
        "AskVol": rec["av"].astype("float64"),
    })


def _get_bi5(url: str, max_retries: int):
    """GET one hourly .bi5; None for 404 / no-data hours. Backoff on transient errors."""
    for attempt in range(1, max_retries + 1):
        try:
            r = _SESSION.get(url, timeout=90)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.content
        except Exception:
            if attempt == max_retries:
                raise
            time.sleep(min(2 ** attempt, 30))
    return None


def fetch_day(day: datetime, max_retries: int = 5) -> pd.DataFrame:
    """Fetch one UTC day of tick (24 hourly .bi5 files). Saturdays are skipped (FX
    closed); empty/absent hours are simply omitted. Hours are appended in order, so
    the day comes out chronologically sorted."""
    if day.weekday() == 5:  # Saturday
        return empty_tick_frame()

    def grab(h: int):
        content = _get_bi5(DUKA_URL.format(y=day.year, mi=day.month - 1,
                                           d=day.day, h=h), max_retries)
        if content is None:
            return None
        return _decode_bi5(content, pd.Timestamp(day.year, day.month, day.day, h))

    # 24 hourly files are pure latency; fetch them concurrently.
    with ThreadPoolExecutor(max_workers=HOUR_WORKERS) as ex:
        parts = list(ex.map(grab, range(24)))  # map preserves hour order
    frames = [f for f in parts if f is not None and len(f)]
    if not frames:
        return empty_tick_frame()
    return pd.concat(frames, ignore_index=True)[TICK_SCHEMA]


def _fetch_one(day: datetime, force: bool):
    """Worker: fetch + atomically write one day. Returns (day, status, err).
    Each day is an independent file/HTTP stream, so this is thread-safe."""
    out = TICK_DIR / f"year={day.year}" / f"{day:%Y-%m-%d}.parquet"
    if not force and parquet_ok(out):
        return (day, "skip", None)
    try:
        df = fetch_day(day)
    except Exception as exc:
        return (day, "fail", f"{type(exc).__name__}: {exc}")
    write_parquet_atomic(df, out)  # empty days written as 0-row files -> resume-safe
    return (day, "empty" if df.empty else "ok", None)


def cmd_fetch(args) -> None:
    """Fetch tick concurrently. The hourly .bi5 files are I/O-latency bound, so a
    thread pool gives a near-linear speedup over the sequential one-day-at-a-time."""
    start, end = args.start, args.end
    days = list(day_range(start, end))
    log(f"FETCH tick {start:%Y-%m-%d} -> {end:%Y-%m-%d}  "
        f"({len(days)} days, {args.workers} workers)")
    missing: list[str] = []
    done = ok = skipped = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_fetch_one, d, args.force) for d in days]
        for fut in as_completed(futs):
            day, status, err = fut.result()
            done += 1
            if status == "skip":
                skipped += 1
            elif status == "fail":
                missing.append(f"{day:%Y-%m-%d} ({err})")
            else:
                ok += 1
            if done % 100 == 0 or done == len(days):
                rate = done / max(time.time() - t0, 1e-9)
                eta = (len(days) - done) / max(rate, 1e-9)
                log(f"  ... {done}/{len(days)}  ok={ok} skip={skipped} "
                    f"fail={len(missing)}  {rate:.1f} day/s  eta {eta/60:.0f}m")
    log(f"FETCH done: {ok} fetched, {skipped} on disk, {len(missing)} failed "
        f"in {(time.time()-t0)/60:.1f} min")
    if missing:
        log("  failed (re-run `fetch` to fill): "
            + ", ".join(missing[:20]) + (" ..." if len(missing) > 20 else ""))


# --------------------------------------------------------------------------- #
# Step 2 — build 1m base (stream tick day-by-day; only 1m is held in memory)
# --------------------------------------------------------------------------- #
def aggregate_1m(tick: pd.DataFrame) -> pd.DataFrame:
    """Tick -> 1m OHLCV on MID price; Volume = sum(BidVol+AskVol)."""
    if tick.empty:
        return pd.DataFrame(columns=BAR_SCHEMA)
    mid = (tick["Bid"].to_numpy() + tick["Ask"].to_numpy()) / 2.0
    g = pd.DataFrame(
        {"mid": mid, "vol": tick["BidVol"].to_numpy() + tick["AskVol"].to_numpy()},
        index=pd.DatetimeIndex(tick["Datetime"]),
    )
    bars = g.resample("1min", closed="left", label="left").agg(
        Open=("mid", "first"), High=("mid", "max"),
        Low=("mid", "min"), Close=("mid", "last"), Volume=("vol", "sum"),
    ).dropna(subset=["Open"])
    return bars.reset_index().rename(columns={"index": "Datetime"})


def iter_tick_files():
    for ydir in sorted(TICK_DIR.glob("year=*")):
        for f in sorted(ydir.glob("*.parquet")):
            yield f


def cmd_build1m(args) -> None:
    files = list(iter_tick_files())
    if not files:
        sys.exit("No tick files found — run `fetch` first.")
    # Staleness check (not just exists): rebuild if any tick file is newer than the
    # 1m base. This makes `all` self-correct after a fresh fetch instead of skipping
    # a 1m base that was built from an older/smaller tick layer.
    newest_tick = max(f.stat().st_mtime for f in files)
    if not args.force and parquet_ok(ONE_MIN_PATH) and ONE_MIN_PATH.stat().st_mtime >= newest_tick:
        log(f"BUILD1M skip (up to date): {ONE_MIN_PATH.name}  (use --force to rebuild)")
        return
    log(f"BUILD1M aggregating tick -> 1m from {len(files)} day files (streaming)")
    frames = []
    for i, f in enumerate(files, 1):
        t = pd.read_parquet(f)
        if not t.empty:
            frames.append(aggregate_1m(t))
        if i % 200 == 0:
            log(f"  ... {i}/{len(files)} days")
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=BAR_SCHEMA)
    df = df.drop_duplicates(subset="Datetime").sort_values("Datetime").reset_index(drop=True)
    write_parquet_atomic(df[BAR_SCHEMA], ONE_MIN_PATH)
    log(f"BUILD1M done: {len(df):,} 1m bars -> {ONE_MIN_PATH.name}")


# --------------------------------------------------------------------------- #
# Step 3 — resample 1m -> higher timeframes (always from the 1m base)
# --------------------------------------------------------------------------- #
def resample_tf(df1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """origin='epoch' anchors bars to UTC boundaries (1d -> UTC midnight,
    4h -> 00/04/08/12/16/20). Empty weekend/holiday bars are dropped."""
    out = (df1m.resample(rule, closed="left", label="left", origin="epoch")
                .agg(BAR_AGG)
                .dropna(subset=["Open"]))
    return out.reset_index()


def cmd_resample(args) -> None:
    if not parquet_ok(ONE_MIN_PATH):
        sys.exit("No 1m base found — run `build1m` first.")
    src_mtime = ONE_MIN_PATH.stat().st_mtime
    df1m = pd.read_parquet(ONE_MIN_PATH).set_index("Datetime").sort_index()
    log(f"RESAMPLE from {len(df1m):,} 1m bars")
    for tf, (stem, rule) in TIMEFRAMES.items():
        out_path = DATA_DIR / f"{stem}.parquet"
        # rebuild if missing OR older than the 1m base it derives from
        if not args.force and parquet_ok(out_path) and out_path.stat().st_mtime >= src_mtime:
            log(f"  {tf:>3}  skip (up to date)")
            continue
        out = resample_tf(df1m, rule)
        write_parquet_atomic(out[BAR_SCHEMA], out_path)
        log(f"  {tf:>3}  {len(out):>8,} bars -> {stem}.parquet")


# --------------------------------------------------------------------------- #
# Quality report
# --------------------------------------------------------------------------- #
def fx_open_mask(idx: pd.DatetimeIndex) -> pd.Series:
    """Conservative FX-session mask. We only mark a minute 'expected open' when it
    is *certainly* in session, so the DST-shifting weekend close (Fri 21:00 vs 22:00
    UTC) never produces false 'missing bar' alarms:
        Mon-Thu        : open all day
        Friday         : open if hour < 21   (never expect the 21-22 close window)
        Saturday       : closed
        Sunday         : open if hour >= 22  (never expect the 21-22 open window)
    Dec 25 and Jan 1 are treated as closed (major FX holidays)."""
    wd, hr = idx.weekday, idx.hour
    open_ = (wd <= 3) | ((wd == 4) & (hr < 21)) | ((wd == 6) & (hr >= 22))
    holiday = ((idx.month == 12) & (idx.day == 25)) | ((idx.month == 1) & (idx.day == 1))
    return pd.Series(open_ & ~holiday, index=idx)


def detect_gaps_1m(df1m: pd.DataFrame) -> list[tuple]:
    """Return (gap_start, gap_end, missing_minutes) for runs of missing in-session
    1m bars, weekend/holiday close excluded."""
    have = pd.DatetimeIndex(df1m["Datetime"])
    full = pd.date_range(have.min(), have.max(), freq="1min")
    expected = full[fx_open_mask(full).to_numpy()]
    missing = expected.difference(have)
    if len(missing) == 0:
        return []
    # group consecutive missing minutes into runs
    runs, s = [], missing[0]
    prev = missing[0]
    for ts in missing[1:]:
        if ts - prev > pd.Timedelta(minutes=1):
            runs.append((s, prev, int((prev - s) / pd.Timedelta(minutes=1)) + 1))
            s = ts
        prev = ts
    runs.append((s, prev, int((prev - s) / pd.Timedelta(minutes=1)) + 1))
    return runs


def integrity_check(df1m: pd.DataFrame, tf_df: pd.DataFrame, rule: str, n: int = 5) -> list[str]:
    """For n random higher-TF bars, assert OHLC matches the underlying 1m slice."""
    issues = []
    offset = pd.tseries.frequencies.to_offset(rule)
    idx1m = pd.DatetimeIndex(df1m["Datetime"])
    base = df1m.set_index(idx1m)
    sample = tf_df.sample(min(n, len(tf_df)), random_state=42)
    for _, bar in sample.iterrows():
        t = bar["Datetime"]
        sl = base.loc[(idx1m >= t) & (idx1m < t + offset)]
        if sl.empty:
            issues.append(f"{rule} bar {t}: no underlying 1m slice")
            continue
        import numpy as np
        checks = {
            "Open": (bar.Open, sl.Open.iloc[0]), "High": (bar.High, sl.High.max()),
            "Low": (bar.Low, sl.Low.min()), "Close": (bar.Close, sl.Close.iloc[-1]),
        }
        for k, (a, b) in checks.items():
            if not np.isclose(a, b, rtol=0, atol=1e-9):
                issues.append(f"{rule} bar {t}: {k} {a} != 1m {b}")
    return issues


def sanity_check(df: pd.DataFrame, is_bar: bool) -> list[str]:
    issues = []
    dt = pd.DatetimeIndex(df["Datetime"])
    if not dt.is_monotonic_increasing:
        issues.append("Datetime not monotonic increasing")
    if dt.has_duplicates:
        issues.append(f"{dt.duplicated().sum()} duplicate timestamps")
    if is_bar:
        for c in ["Open", "High", "Low", "Close"]:
            if df[c].isna().any():
                issues.append(f"NaN in {c}")
        bad = (df["High"] < df[["Open", "Close"]].max(axis=1)) | \
              (df["Low"] > df[["Open", "Close"]].min(axis=1))
        if bad.any():
            issues.append(f"{int(bad.sum())} bars violate High>=max(O,C)/Low<=min(O,C)")
    return issues


def cmd_report(args) -> None:
    lines = ["# XAU/USD dataset quality report",
             f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}_", ""]

    def emit(s=""):
        print(s)
        lines.append(s)

    # --- raw tick (read only the Datetime column via the partitioned dataset) ---
    emit("## Files")
    if TICK_DIR.exists():
        # count via row-group metadata (no data read); min/max from the first/last
        # non-empty day file (files are chronological) — avoids loading ~400M rows.
        ds = pads.dataset(TICK_DIR, format="parquet", partitioning="hive")
        nrows = ds.count_rows()
        files = list(iter_tick_files())
        tmin = tmax = "—"
        for f in files:
            d = pd.read_parquet(f, columns=["Datetime"])
            if len(d):
                tmin = d["Datetime"].iloc[0]
                break
        for f in reversed(files):
            d = pd.read_parquet(f, columns=["Datetime"])
            if len(d):
                tmax = d["Datetime"].iloc[-1]
                break
        emit(f"- **xauusd_tick** (raw): {nrows:,} ticks | {tmin} -> {tmax}")

    bar_files = {"1m": ONE_MIN_PATH, **{tf: DATA_DIR / f"{s}.parquet"
                                        for tf, (s, _) in TIMEFRAMES.items()}}
    loaded = {}
    for tf, path in bar_files.items():
        if not parquet_ok(path):
            emit(f"- **{path.name}**: MISSING")
            continue
        df = pd.read_parquet(path)
        loaded[tf] = df
        size_mb = path.stat().st_size / 1e6
        emit(f"- **{path.name}**: {len(df):,} bars | "
             f"{df.Datetime.min()} -> {df.Datetime.max()} | {size_mb:.1f} MB")

    # --- 1m gap detection (weekend-aware) ---
    emit("\n## 1m gap detection (FX weekend/holiday excluded)")
    if "1m" in loaded:
        runs = detect_gaps_1m(loaded["1m"])
        if not runs:
            emit("- OK — no in-session gaps")
        else:
            tot = sum(r[2] for r in runs)
            emit(f"- {len(runs)} gap run(s), {tot:,} missing in-session minutes total. Largest:")
            for s, e, n in sorted(runs, key=lambda r: -r[2])[:10]:
                emit(f"    - {n:>5} min  {s} -> {e}")

    # --- resample integrity ---
    emit("\n## Resample integrity (5 random bars/TF vs underlying 1m)")
    if "1m" in loaded:
        all_ok = True
        for tf, (stem, rule) in TIMEFRAMES.items():
            if tf not in loaded:
                continue
            issues = integrity_check(loaded["1m"], loaded[tf], rule)
            if issues:
                all_ok = False
                for i in issues:
                    emit(f"    - FAIL {i}")
            else:
                emit(f"- {tf:>3}: OK")
        if all_ok:
            emit("- all timeframes reconcile exactly")

    # --- global sanity ---
    emit("\n## Sanity (ordering / dupes / NaN / OHLC bounds)")
    for tf, df in loaded.items():
        issues = sanity_check(df, is_bar=True)
        emit(f"- {tf:>3}: " + ("OK" if not issues else "; ".join(issues)))

    REPORT_PATH.write_text("\n".join(lines) + "\n")
    log(f"REPORT written -> {REPORT_PATH.name}")


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #
def write_manifest(start, end):
    import numpy as np
    man = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Dukascopy tick via dukascopy-python",
        "instrument": "XAU/USD",
        "price_basis": "mid=(bid+ask)/2",
        "bar_boundary": "closed='left', label='left' (timestamp = bar open)",
        "timezone": "UTC, tz-naive",
        "range_requested": [start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")],
        "versions": {"pandas": pd.__version__, "dukascopy_python": "4.0.1",
                     "python": sys.version.split()[0]},
        "files": {},
    }
    for tf, (stem, _) in [("1m", ("xauusd_1m", None)), *TIMEFRAMES.items()]:
        p = ONE_MIN_PATH if tf == "1m" else DATA_DIR / f"{stem}.parquet"
        if parquet_ok(p):
            df = pd.read_parquet(p, columns=["Datetime"])
            man["files"][p.name] = {
                "rows": len(df),
                "min": str(df.Datetime.min()), "max": str(df.Datetime.max()),
                "bytes": p.stat().st_size,
            }
    MANIFEST_PATH.write_text(json.dumps(man, indent=2))
    log(f"MANIFEST written -> {MANIFEST_PATH.name}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def resolve_window(args):
    """Default full run: DUKA_START -> yesterday (exclude in-progress UTC day)."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    if args.sample:
        end = today
        start = end - timedelta(days=31)
    else:
        start = parse_day(args.start) if args.start else DEFAULT_START
        end = parse_day(args.end) if args.end else today
    return start, end


# --------------------------------------------------------------------------- #
# Fast path — daily 1m CANDLE files (24x fewer requests than hourly tick .bi5)
# One BID + one ASK file per day; mid = (bid+ask)/2 field-wise. Use when the tick
# route is too slow (cold CDN): `fetchm1` fills a per-day cache, `buildm1c`
# assembles xauusd_1m.parquet from it (then run `resample`). Bars are identical
# in role to tick-aggregated 1m; H/L are mid-of-extremes (≤ half-spread apart).
# --------------------------------------------------------------------------- #
M1C_URL = ("https://datafeed.dukascopy.com/datafeed/XAUUSD/{y}/{mi:02d}/{d:02d}/"
           "{side}_candles_min_1.bi5")
M1C_DIR = DATA_DIR / "xauusd_m1_daily"
_M1C_DTYPE = np.dtype([("sec", ">u4"), ("o", ">u4"), ("c", ">u4"),
                       ("l", ">u4"), ("h", ">u4"), ("v", ">f4")])

def _decode_m1c(content: bytes, day_start: pd.Timestamp):
    if not content:
        return None
    rec = np.frombuffer(lzma.decompress(content), dtype=_M1C_DTYPE)
    if rec.size == 0:
        return None
    return pd.DataFrame({
        "Datetime": day_start + pd.to_timedelta(rec["sec"].astype("int64"), unit="s"),
        "Open": rec["o"] / POINT, "High": rec["h"] / POINT,
        "Low": rec["l"] / POINT, "Close": rec["c"] / POINT,
        "Volume": rec["v"].astype("float64"),
    })

def _fetch_m1c_day(day: datetime, force: bool):
    out = M1C_DIR / f"year={day.year}" / f"{day:%Y-%m-%d}.parquet"
    if not force and parquet_ok(out):
        return (day, "skip", None)
    if day.weekday() == 5:
        write_parquet_atomic(pd.DataFrame(columns=BAR_SCHEMA), out)
        return (day, "empty", None)
    try:
        d0 = pd.Timestamp(day.year, day.month, day.day)
        sides = {}
        for side in ("BID", "ASK"):
            content = _get_bi5(M1C_URL.format(y=day.year, mi=day.month - 1,
                                              d=day.day, side=side), 5)
            sides[side] = _decode_m1c(content, d0) if content else None
        b, a = sides["BID"], sides["ASK"]
        if b is None and a is None:
            df = pd.DataFrame(columns=BAR_SCHEMA)
        elif b is None or a is None:
            df = (b if a is None else a)[BAR_SCHEMA]
        else:
            m = b.merge(a, on="Datetime", suffixes=("_b", "_a"), how="outer").sort_values("Datetime")
            df = pd.DataFrame({"Datetime": m["Datetime"]})
            for f_ in ("Open", "High", "Low", "Close"):
                df[f_] = m[[f"{f_}_b", f"{f_}_a"]].mean(axis=1)
            df["Volume"] = m[["Volume_b", "Volume_a"]].sum(axis=1)
    except Exception as exc:
        return (day, "fail", f"{type(exc).__name__}: {exc}")
    write_parquet_atomic(df[BAR_SCHEMA], out)
    return (day, "ok" if len(df) else "empty", None)

def cmd_fetchm1(args) -> None:
    days = list(day_range(args.start, args.end))
    log(f"FETCH m1-candles {args.start:%Y-%m-%d} -> {args.end:%Y-%m-%d}  "
        f"({len(days)} days x2 files, {args.workers} workers)")
    missing, done, ok, skipped, t0 = [], 0, 0, 0, time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = [ex.submit(_fetch_m1c_day, d, args.force) for d in days]
        for fut in as_completed(futs):
            day, status, err = fut.result()
            done += 1
            if status == "skip":
                skipped += 1
            elif status == "fail":
                missing.append(f"{day:%Y-%m-%d} ({err})")
            else:
                ok += 1
            if done % 100 == 0 or done == len(days):
                rate = done / max(time.time() - t0, 1e-9)
                log(f"  ... {done}/{len(days)}  ok={ok} skip={skipped} "
                    f"fail={len(missing)}  {rate:.1f} day/s  "
                    f"eta {(len(days)-done)/max(rate,1e-9)/60:.0f}m")
    log(f"FETCHM1 done: {ok} fetched, {skipped} on disk, {len(missing)} failed")
    if missing:
        log("  failed (re-run to fill): " + ", ".join(missing[:20]))

def cmd_buildm1c(args) -> None:
    files = [f for ydir in sorted(M1C_DIR.glob("year=*")) for f in sorted(ydir.glob("*.parquet"))]
    if not files:
        sys.exit("No m1-candle files — run `fetchm1` first.")
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.dropna(subset=["Open"]).sort_values("Datetime").drop_duplicates("Datetime")
    write_parquet_atomic(df[BAR_SCHEMA], ONE_MIN_PATH)
    log(f"BUILDM1C done: {len(df):,} 1m bars -> {ONE_MIN_PATH}")

def main():
    ap = argparse.ArgumentParser(description="XAU/USD tick->bar Parquet pipeline.")
    ap.add_argument("command", choices=["fetch", "build1m", "resample", "report", "all",
                                        "fetchm1", "buildm1c"])
    ap.add_argument("--start", help="YYYY-MM-DD (default 2003-05-04)")
    ap.add_argument("--end", help="YYYY-MM-DD exclusive (default: today, partial day excluded)")
    ap.add_argument("--sample", action="store_true", help="last ~31 days (quick validation)")
    ap.add_argument("--force", action="store_true", help="refetch/rebuild existing files")
    ap.add_argument("--workers", type=int, default=10, help="parallel fetch workers (default 10)")
    args = ap.parse_args()
    args.start, args.end = resolve_window(args)

    DATA_DIR.mkdir(exist_ok=True)
    if args.command == "fetchm1":
        cmd_fetchm1(args)
        return
    if args.command == "buildm1c":
        cmd_buildm1c(args)
        return
    if args.command in ("fetch", "all"):
        cmd_fetch(args)
    if args.command in ("build1m", "all"):
        cmd_build1m(args)
    if args.command in ("resample", "all"):
        cmd_resample(args)
    if args.command in ("report", "all"):
        cmd_report(args)
        write_manifest(args.start, args.end)


if __name__ == "__main__":
    main()
