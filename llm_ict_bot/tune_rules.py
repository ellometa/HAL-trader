"""Rule-strategy tuning harness.

Methodology (anti-overfitting contract):
  * TUNE window 2021-01-01 → 2025-01-01: the parameter grid runs ONLY here.
  * OOS window 2025-01-01 → 2026-06-06: untouched until a shortlist exists; finalists
    run there once, through this same evaluator AND the real engine in the notebook.
  * A config is shortlisted on robustness (positive avg R in >= 3 of 4 tune years,
    enough trades), never on aggregate return alone.

The evaluator mirrors make_rule_decide_fn() exactly at default parameters
(15m sweep -> 15m structure shift after the sweep -> HTF premium/discount filter,
SL beyond the sweep wick, TP at RR_TARGET, next-1m-open fills, SL-first pessimism,
flat round-trip spread) but exposes the knobs as a RuleParams dataclass and runs
~1000x faster via presorted numpy lookups. Official numbers for finalists still
come from the notebook engine.

Usage:
  ../.venv/bin/python tune_rules.py --window tune          # full grid, CSV out
  ../.venv/bin/python tune_rules.py --window tune --quick  # sanity subset
  ../.venv/bin/python tune_rules.py --window oos --configs runs/tune/shortlist.json
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent

# ---------------------------------------------------------------- bootstrap
# Reuse the notebook's own config, data pipeline and detectors so definitions
# cannot drift. part2 loads/cleans the full 1m store; part3 defines detectors.
_g = globals()
for _part in ("part1_config_data.py", "part2_resample_load.py", "part3_detectors.py"):
    exec(compile((HERE / _part).read_text(), _part, "exec"), _g)
# in_ny_session + its session-minute constants live in part5a (which drags in the
# whole engine); extract just that prelude rather than exec'ing the engine.
import re as _re
_p5a = (HERE / "part5a_simulator.py").read_text()
_m = _re.search(r"(def _session_minutes.*?return np\.asarray\(.*?\)\n)", _p5a, _re.S)
exec(compile(_m.group(1), "part5a_simulator.py:session", "exec"), _g)
RULE_SL_BUFFER_PIPS = 2.0      # mirrors part5b

TUNE_START, TUNE_END = pd.Timestamp("2021-01-01"), pd.Timestamp("2025-01-01")
OOS_START,  OOS_END  = pd.Timestamp("2025-01-01"), pd.Timestamp("2026-06-06")
WARMUP = pd.Timedelta(days=60)
FAR_PAST = np.datetime64("2000-01-01")   # "since" floor for most-recent-state lookups
TUNE_DIR = OUT_DIR / "tune"
TUNE_DIR.mkdir(parents=True, exist_ok=True)


def context_week_start(t: pd.Timestamp) -> pd.Timestamp:
    """Monday 00:00 UTC of the previous ISO week (mirrors part4.context_window_start)."""
    return (t - pd.Timedelta(days=int(t.dayofweek))).normalize() - pd.Timedelta(weeks=1)


# ---------------------------------------------------------------- market prep (cached)
def prepare_window(start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """1m slice + 15m/1h/4h frames + detector records for [start - warmup, end)."""
    key = TUNE_DIR / f"prep_{start.date()}_{end.date()}.pkl"
    if key.exists():
        with open(key, "rb") as f:
            return pickle.load(f)
    t0 = time.time()
    m1 = clean_1m[(clean_1m.index >= start - WARMUP) & (clean_1m.index < end)]
    tfs = build_tf_store(m1, ["15min", "1h", "4h"])
    dets = {tf: run_all_detectors(tfs[tf]) for tf in tfs}
    out = {"m1": m1, "tfs": tfs, "dets": dets}
    with open(key, "wb") as f:
        pickle.dump(out, f)
    print(f"prepared {start.date()} → {end.date()} in {time.time()-t0:.0f}s "
          f"({len(m1):,} 1m bars) — cached to {key.name}")
    return out


# ---------------------------------------------------------------- fast lookup tables
class EventTable:
    """Events sorted by `time`, with a suffix-min over confirmed_time so
    'exists event with time > T0 confirmed by t' is O(log n)."""

    def __init__(self, df: pd.DataFrame, cols: tuple[str, ...] = ()):
        df = df.sort_values("time", kind="mergesort")
        self.time = df["time"].to_numpy(dtype="datetime64[ns]")
        self.conf = df["confirmed_time"].to_numpy(dtype="datetime64[ns]")
        self.cols = {c: df[c].to_numpy() for c in cols}
        self.sufmin_conf = (np.minimum.accumulate(self.conf[::-1])[::-1]
                            if len(self.conf) else self.conf)

    def any_after(self, t0: np.datetime64, t: np.datetime64) -> bool:
        """True iff some event has time > t0 and confirmed_time <= t."""
        i = np.searchsorted(self.time, t0, side="right")
        return i < len(self.time) and self.sufmin_conf[i] <= t

    def last_visible(self, t: np.datetime64, since: np.datetime64):
        """Index of the latest event with time in [since, t] and confirmed_time <= t."""
        hi = np.searchsorted(self.time, t, side="right")
        lo = np.searchsorted(self.time, since, side="left")
        for i in range(hi - 1, lo - 1, -1):
            if self.conf[i] <= t:
                return i
        return None


def build_tables(prep: dict) -> dict:
    d15 = prep["dets"]["15min"]
    tabs = {
        "sweeps": EventTable(d15["sweeps"], ("side", "wick_extreme")),
        "struct_up": EventTable(d15["structure"][d15["structure"]["kind"]
                                .isin(["CHoCH_up", "BOS_up"])]),
        "struct_dn": EventTable(d15["structure"][d15["structure"]["kind"]
                                .isin(["CHoCH_down", "BOS_down"])]),
        "choch_up": EventTable(d15["structure"][d15["structure"]["kind"] == "CHoCH_up"]),
        "choch_dn": EventTable(d15["structure"][d15["structure"]["kind"] == "CHoCH_down"]),
        "fvg_up": EventTable(d15["fvg"][d15["fvg"]["direction"] == "bullish"]),
        "fvg_dn": EventTable(d15["fvg"][d15["fvg"]["direction"] == "bearish"]),
        "disp_up": EventTable(d15["displacement"][d15["displacement"]["direction"] == "bullish"]),
        "disp_dn": EventTable(d15["displacement"][d15["displacement"]["direction"] == "bearish"]),
    }
    for tf in ("15min", "1h", "4h"):
        sw = prep["dets"][tf]["swings"]
        tabs[f"swing_hi_{tf}"] = EventTable(sw[sw["kind"] == "high"], ("level",))
        tabs[f"swing_lo_{tf}"] = EventTable(sw[sw["kind"] == "low"], ("level",))
    for tf in ("1h", "4h"):              # HTF trend state (+1/-1) for the bias filter
        st = prep["dets"][tf]["structure"]
        tabs[f"trend_{tf}"] = EventTable(st[st["trend_after"].notna()], ("trend_after",))
    return tabs


# ---------------------------------------------------------------- parameters
@dataclasses.dataclass(frozen=True)
class RuleParams:
    # strategy family:
    #   "reversal" — fade a sweep: sweep -> opposite structure shift -> trade reversion
    #                (stop beyond the sweep wick). The shipped rule_ict.
    #   "breakout" — trade WITH momentum: a recent 15m displacement sets direction,
    #                trade its way, stop beyond the last opposing 15m swing.
    strategy: str = "reversal"
    sweep_window_h: float = 3.0       # signal lookback (sweep age, or displacement age)
    killzone_only: bool = False       # decisions 07:00-10:00 ET only (vs full session)
    shift_kinds: str = "both"         # "both" = CHoCH+BOS | "choch" = CHoCH only
    pd_tf: str = "1h"                 # premium/discount source: "1h"|"4h"|"off"
    need_fvg: bool = False            # require a same-direction 15m FVG after the signal
    need_disp: bool = False           # require a (further) same-dir displacement after signal
    rr_target: float = 2.0            # TP at rr_target * risk
    min_stop_pips: float = 5.0        # reject thinner stops
    htf_bias: str = "off"             # require trade WITH this TF's trend: "off"|"1h"|"4h"
                                      # (proper ICT only trades with higher-timeframe bias)

    def label(self) -> str:
        fam = "REV" if self.strategy == "reversal" else "BRK"
        return (f"{fam}_W{self.sweep_window_h:g}h_{'KZ' if self.killzone_only else 'SES'}_"
                f"{self.shift_kinds}_pd{self.pd_tf}_fvg{int(self.need_fvg)}_"
                f"disp{int(self.need_disp)}_rr{self.rr_target:g}_ms{self.min_stop_pips:g}_"
                f"bias{self.htf_bias}")


BASELINE = RuleParams()   # exactly the shipped rule_ict


# ---------------------------------------------------------------- evaluator
def evaluate(prep: dict, tabs: dict, p: RuleParams,
             start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """Sequential one-position walk over NY-session 15m closes; returns trades + stats."""
    f15 = prep["tfs"]["15min"]
    ct = pd.DatetimeIndex(f15["close_time"])
    in_win = (ct > start) & (ct <= end) & in_ny_session(ct)
    if p.killzone_only:
        et_h = ct.tz_localize("UTC").tz_convert("America/New_York").hour
        in_win &= np.asarray((et_h >= 7) & (et_h < 10))
    slots = f15[in_win]
    slot_t = pd.DatetimeIndex(slots["close_time"]).to_numpy()
    slot_px = slots["close"].to_numpy()

    m1 = prep["m1"]
    m1_t = m1.index.to_numpy()
    m1_o, m1_h, m1_l = (m1[c].to_numpy() for c in ("open", "high", "low"))

    sw = tabs["sweeps"]
    week_starts = np.array([context_week_start(pd.Timestamp(t)) for t in slot_t],
                           dtype="datetime64[ns]")
    sig_w = np.timedelta64(int(p.sweep_window_h * 3600), "s")
    spread = SPREAD_PIPS * PIP

    trades = []
    busy_until = np.datetime64("1970-01-01")
    for k in range(len(slot_t)):
        t = slot_t[k]
        if t <= busy_until:
            continue
        price = slot_px[k]

        # --- signal: direction (want), the confluence anchor time, and the stop anchor
        if p.strategy == "reversal":
            i = sw.last_visible(t, t - sig_w)
            if i is None:
                continue
            anchor_t, side, wick = sw.time[i], sw.cols["side"][i], sw.cols["wick_extreme"][i]
            want = "long" if side == "sellside" else "short"
            sl_ref = wick                                     # stop beyond the sweep wick
        else:  # breakout — most recent 15m displacement within the window sets direction
            iu = tabs["disp_up"].last_visible(t, t - sig_w)
            idn = tabs["disp_dn"].last_visible(t, t - sig_w)
            cand = []
            if iu is not None:
                cand.append((tabs["disp_up"].time[iu], "long"))
            if idn is not None:
                cand.append((tabs["disp_dn"].time[idn], "short"))
            if not cand:
                continue
            anchor_t, want = max(cand, key=lambda x: x[0])    # the latest displacement
            stab = tabs[f"swing_lo_15min" if want == "long" else "swing_hi_15min"]
            si = stab.last_visible(t, week_starts[k])         # opposing 15m swing = stop
            if si is None:
                continue
            sl_ref = stab.cols["level"][si]

        # higher-timeframe bias: only trade WITH the HTF trend (proper-ICT requirement)
        if p.htf_bias != "off":
            tt = tabs[f"trend_{p.htf_bias}"]
            ti = tt.last_visible(t, FAR_PAST)
            if ti is None:
                continue
            trend = tt.cols["trend_after"][ti]
            if (want == "long" and trend != 1) or (want == "short" and trend != -1):
                continue

        if p.shift_kinds == "both":
            stab = tabs["struct_up" if want == "long" else "struct_dn"]
        else:
            stab = tabs["choch_up" if want == "long" else "choch_dn"]
        if not stab.any_after(anchor_t, t):
            continue
        if p.need_fvg and not tabs["fvg_up" if want == "long" else "fvg_dn"].any_after(anchor_t, t):
            continue
        if p.need_disp and not tabs["disp_up" if want == "long" else "disp_dn"].any_after(anchor_t, t):
            continue
        if p.pd_tf != "off":
            hi_i = tabs[f"swing_hi_{p.pd_tf}"].last_visible(t, week_starts[k])
            lo_i = tabs[f"swing_lo_{p.pd_tf}"].last_visible(t, week_starts[k])
            if hi_i is None or lo_i is None:
                continue
            hi = tabs[f"swing_hi_{p.pd_tf}"].cols["level"][hi_i]
            lo = tabs[f"swing_lo_{p.pd_tf}"].cols["level"][lo_i]
            if hi <= lo:
                continue
            pos = (price - lo) / (hi - lo)
            if want == "long" and not pos < 0.45:        # discount (eq band 0.05)
                continue
            if want == "short" and not pos > 0.55:       # premium
                continue

        sl = sl_ref - RULE_SL_BUFFER_PIPS * PIP if want == "long" \
            else sl_ref + RULE_SL_BUFFER_PIPS * PIP
        risk = (price - sl) if want == "long" else (sl - price)
        if risk < p.min_stop_pips * PIP:        # also rejects wrong-side stops (risk<=0)
            continue

        # fill at next 1m open; SL-first pessimism inside each bar
        j0 = np.searchsorted(m1_t, t, side="left")     # first bar opening at/after t
        if j0 >= len(m1_t):
            break
        fill = m1_o[j0]
        risk_f = (fill - sl) if want == "long" else (sl - fill)
        if risk_f <= 0:
            continue                                    # gapped through the stop
        tp = fill + p.rr_target * risk_f if want == "long" else fill - p.rr_target * risk_f

        exit_r, exit_j = None, None
        for j in range(j0, min(j0 + 40_000, len(m1_t))):
            if want == "long":
                if m1_l[j] <= sl:
                    exit_r, exit_j = -1.0, j; break
                if m1_h[j] >= tp:
                    exit_r, exit_j = p.rr_target, j; break
            else:
                if m1_h[j] >= sl:
                    exit_r, exit_j = -1.0, j; break
                if m1_l[j] <= tp:
                    exit_r, exit_j = p.rr_target, j; break
        if exit_r is None:
            break                                       # ran off the data end
        r_net = exit_r - spread / risk_f
        trades.append({"entry_time": pd.Timestamp(t), "direction": want,
                       "r": r_net, "year": pd.Timestamp(t).year})
        busy_until = m1_t[exit_j]

    tr = pd.DataFrame(trades)
    out = {"label": p.label(), "params": dataclasses.asdict(p), "trades": len(tr)}
    if len(tr):
        r = tr["r"]
        wins, losses = r[r > 0], r[r <= 0]
        out.update(avg_r=round(r.mean(), 4), win_rate=round(100 * (r > 0).mean(), 1),
                   pf=round(wins.sum() / abs(losses.sum()), 3) if len(losses) and losses.sum() != 0 else float("inf"),
                   total_r=round(r.sum(), 2))
        by_year = tr.groupby("year")["r"].agg(["sum", "count"])
        out["years_pos"] = int((by_year["sum"] > 0).sum())
        out["year_detail"] = {int(y): (round(v["sum"], 2), int(v["count"]))
                              for y, v in by_year.iterrows()}
        out["_trades_df"] = tr
    else:
        out.update(avg_r=np.nan, win_rate=np.nan, pf=np.nan, total_r=0.0,
                   years_pos=0, year_detail={})
    return out


# ---------------------------------------------------------------- grid
def full_grid(strategy: str = "reversal") -> list[RuleParams]:
    g = itertools.product(
        (2.0, 3.0, 6.0),          # sweep_window_h (signal lookback)
        (False, True),            # killzone_only
        ("both", "choch"),        # shift_kinds
        ("1h", "4h", "off"),      # pd_tf
        (False, True),            # need_fvg
        (False, True),            # need_disp
        (1.0, 1.5, 2.0, 3.0),     # rr_target
        (5.0, 8.0),               # min_stop_pips
        ("off", "1h", "4h"),      # htf_bias — trade with higher-timeframe trend
    )
    return [RuleParams(strategy, *c) for c in g]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", choices=("tune", "oos"), default="tune")
    ap.add_argument("--family", choices=("reversal", "breakout"), default="reversal")
    ap.add_argument("--quick", action="store_true", help="baseline + a few variants only")
    ap.add_argument("--configs", help="JSON file of param dicts (for --window oos)")
    args = ap.parse_args()

    start, end = (TUNE_START, TUNE_END) if args.window == "tune" else (OOS_START, OOS_END)
    print(f"window: {args.window} {start.date()} → {end.date()}")
    prep = prepare_window(start, end)
    tabs = build_tables(prep)

    base = dataclasses.replace(BASELINE, strategy=args.family)
    if args.configs:
        cfgs = [RuleParams(**d) for d in json.load(open(args.configs))]
    elif args.quick:
        cfgs = [base,
                dataclasses.replace(base, killzone_only=True),
                dataclasses.replace(base, need_disp=True),
                dataclasses.replace(base, pd_tf="4h")]
    else:
        cfgs = full_grid(args.family)
    print(f"evaluating {len(cfgs)} {args.family} configs...")

    t0, rows = time.time(), []
    for n, p in enumerate(cfgs, 1):
        res = evaluate(prep, tabs, p, start, end)
        res.pop("_trades_df", None)
        rows.append(res)
        if n % 100 == 0 or n == len(cfgs):
            print(f"  {n}/{len(cfgs)} ({time.time()-t0:.0f}s)")
    df = pd.DataFrame(rows).drop(columns=["params"])
    out_csv = TUNE_DIR / f"grid_{args.family}_{args.window}.csv"
    pd.DataFrame(rows).to_csv(out_csv, index=False)
    print(f"\nsaved {out_csv}")

    print(f"\n=== {args.family} baseline ===")
    print(df[df["label"] == base.label()].to_string(index=False))
    keep = df[(df["trades"] >= 40) & (df["years_pos"] >= 3) & (df["pf"] >= 1.15)] \
        if args.window == "tune" else df
    print(f"\n=== robust configs (trades>=40, years_pos>=3, pf>=1.15): {len(keep)} ===")
    if len(keep):
        print(keep.sort_values("total_r", ascending=False).head(15).to_string(index=False))


if __name__ == "__main__":
    main()
