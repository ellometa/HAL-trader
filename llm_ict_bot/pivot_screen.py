"""Test 9 — evidence-backed pivot screen: replicate published strategies verbatim.

After 8 tests showed mechanized ICT has no edge on EUR/USD 15m, this screen pivots
instrument + strategy family to the ones with *published, audited* backtests, and
replicates the published rules VERBATIM on our own data. No parameter tuning by us —
if a strategy needs our tuning to work, it failed the replication.

Strategies & sources
--------------------
BTC (BTCUSDT 15m → 1h/1d, cost 10 bps/side — Binance taker fee + slippage):
  btc_ma_20_100   Long when 20d MA > 100d MA, else cash. Next-day-open fills.
                  [Grayscale Research, "The Trend is Your Friend"]
  btc_d1h1_macd   Long at next 1h open when hourly MACD(12,26,9) crosses above its
                  signal AND daily MACD line > signal; exit at next 1h open after the
                  first negative hourly bar (close < open).
                  [Quantpedia, "Simple Multi-Timeframe Trend Strategy on Bitcoin";
                   theirs adds a trailing stop — base model replicated here]
SPY daily, 1993+ (cost 1 bp/side; close fills — matches the published convention):
  spy_rsi2        Buy close when RSI(2) < 10, sell close when RSI(2) > 80.
                  [QuantifiedStrategies RSI-2 backtest: ~9%/yr, 28% exposure]
  spy_rsi2_200ma  Classic Connors book variant: same, but only above the 200d MA,
                  exit when close > 5d MA. [Connors & Alvarez, "Short Term Trading
                  Strategies That Work"]
  spy_tt          Turnaround Tuesday: Monday close < Friday close → buy Monday close;
                  exit when close > yesterday's high, or after 5 trading days.
                  [QuantifiedStrategies free strategies list]
  spy_eom         End-of-month: buy close of 5th-last trading day of the month, sell
                  close of the 3rd trading day of the next month. [same source]
QQQ daily, 1999+ (robustness cross-check, not a new strategy):
  qqq_rsi2        spy_rsi2 rules unchanged on QQQ.

Honesty rules
-------------
- Full-period stats AND 2025-01+ sub-period (post-dates most publications — the
  closest thing to out-of-sample replication has).
- Costs: BTC 10 bps/side, equities 1 bp/side. Published numbers are mostly
  frictionless; ours are net.
- SPY/QQQ prices are dividend-adjusted (yfinance auto_adjust) — so is buy-and-hold.
- The QQQ day-of-week strategy (76% win, PF 2.7) was NOT replicated: exact rules are
  paywalled. We do not guess at rules and call it replication.

Run:  .venv/bin/python llm_ict_bot/pivot_screen.py   (from the HAL repo root)
"""
from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
OOS_START = pd.Timestamp("2025-01-01")     # post-publication sub-period
COST = {"BTC": 0.0010, "SPY": 0.0001, "QQQ": 0.0001}   # per side, fraction

# --- data -----------------------------------------------------------------------------
def load_btc() -> tuple[pd.DataFrame, pd.DataFrame]:
    df = pd.read_excel(ROOT / "data/BTCUSDT_15m.xlsx").set_index("Datetime")
    df.columns = [c.lower() for c in df.columns]
    o = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    h1 = df.resample("1h").agg(o).dropna()
    d1 = df.resample("1D").agg(o).dropna()
    return h1, d1

def load_equity(sym: str) -> pd.DataFrame:
    df = pd.read_csv(ROOT / f"data/{sym.lower()}_daily.csv", parse_dates=["date"])
    return df.set_index("date")

# --- indicators (standard definitions, no variants) ------------------------------------
def rsi(close: pd.Series, period: int) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = up / dn
    return 100 - 100 / (1 + rs)

def macd(close: pd.Series, fast=12, slow=26, sig=9) -> tuple[pd.Series, pd.Series]:
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    return line, line.ewm(span=sig, adjust=False).mean()

# --- engine: position series → net equity curve -----------------------------------------
def run(pos: pd.Series, px: pd.DataFrame, cost: float, fill: str) -> dict:
    """pos[t] = target position (0/1) decided at close t. fill='close' → enter that
    close (published equity convention); fill='open' → next bar's open (conservative)."""
    pos = pos.fillna(0.0).astype(float)
    if fill == "close":
        r = px["close"].pct_change().shift(-1)               # close t → close t+1
        held = pos                                           # held over that interval
    else:                                                    # next-open fills
        op = px["open"]
        r = (op.shift(-2) / op.shift(-1) - 1)                # open t+1 → open t+2
        held = pos
    gross = held * r
    turn = held.diff().abs().fillna(held.abs())              # position changes
    net = gross - turn * cost
    eq = (1 + net.fillna(0)).cumprod()
    return {"pos": held, "net": net.fillna(0), "eq": eq}

def trade_stats(pos: pd.Series, net: pd.Series) -> tuple[int, float, float]:
    """Segment consecutive held runs into trades; return (n, win rate, profit factor)."""
    grp = (pos.ne(pos.shift())).cumsum()
    rets = [(1 + net[m]).prod() - 1 for _, m in
            ((g, (grp == g) & (pos > 0)) for g in grp.unique()) if m.any()]
    if not rets:
        return 0, np.nan, np.nan
    w = [x for x in rets if x > 0]; l = [x for x in rets if x <= 0]
    pf = (sum(w) / abs(sum(l))) if l and sum(l) != 0 else np.inf
    return len(rets), len(w) / len(rets), pf

def metrics(res: dict, label: str, ppy: float) -> dict:
    def block(sl):
        net, eq0 = res["net"][sl], (1 + res["net"][sl]).cumprod()
        if len(net) < 2 or eq0.iloc[-1] <= 0:
            return {}
        yrs = len(net) / ppy
        cagr = eq0.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else np.nan
        sharpe = net.mean() / net.std() * np.sqrt(ppy) if net.std() > 0 else np.nan
        dd = (eq0 / eq0.cummax() - 1).min()
        n, wr, pf = trade_stats(res["pos"][sl], net)
        exp = (res["pos"][sl] > 0).mean()
        return {"cagr": cagr, "sharpe": sharpe, "maxdd": dd, "trades": n,
                "win": wr, "pf": pf, "exposure": exp}
    full = block(slice(None))
    oos = block(res["net"].index >= OOS_START)
    return {"label": label, "full": full, "oos": oos, "eq": res["eq"]}

# --- strategies (published rules verbatim) ----------------------------------------------
def btc_ma_20_100(d1: pd.DataFrame) -> dict:
    ma20, ma100 = d1["close"].rolling(20).mean(), d1["close"].rolling(100).mean()
    pos = (ma20 > ma100).astype(float)
    return run(pos, d1, COST["BTC"], fill="open")

def btc_d1h1_macd(h1: pd.DataFrame, d1: pd.DataFrame) -> dict:
    hl, hs = macd(h1["close"])
    dl, ds = macd(d1["close"])
    # daily filter as known at each hourly close: last *completed* daily bar
    day_ok = (dl > ds)
    day_ok.index = day_ok.index + pd.Timedelta(days=1)       # completed next midnight
    ok_h = day_ok.reindex(h1.index, method="ffill").fillna(False)
    entry = (hl > hs) & (hl.shift() <= hs.shift()) & ok_h
    exit_ = h1["close"] < h1["open"]                         # first negative hourly bar
    pos = pd.Series(np.nan, index=h1.index)
    pos[entry] = 1.0
    pos[exit_] = 0.0
    pos = pos.ffill().fillna(0.0)
    return run(pos, h1, COST["BTC"], fill="open")

def rsi2_plain(px: pd.DataFrame, sym: str) -> dict:
    r2 = rsi(px["close"], 2)
    pos = pd.Series(np.nan, index=px.index)
    pos[r2 < 10] = 1.0
    pos[r2 > 80] = 0.0
    return run(pos.ffill().fillna(0.0), px, COST[sym], fill="close")

def spy_rsi2_200ma(px: pd.DataFrame) -> dict:
    r2, ma200, ma5 = rsi(px["close"], 2), px["close"].rolling(200).mean(), px["close"].rolling(5).mean()
    pos = pd.Series(np.nan, index=px.index)
    pos[(r2 < 10) & (px["close"] > ma200)] = 1.0
    pos[px["close"] > ma5] = 0.0
    return run(pos.ffill().fillna(0.0), px, COST["SPY"], fill="close")

def spy_turnaround_tuesday(px: pd.DataFrame) -> dict:
    monday_down = (px.index.dayofweek == 0) & (px["close"] < px["close"].shift())
    exit_sig = px["close"] > px["high"].shift()
    pos = np.zeros(len(px)); held = 0
    md, ex = monday_down.to_numpy(), exit_sig.to_numpy()
    for i in range(len(px)):
        if held == 0 and md[i]:
            held = 1                                          # buy Monday close
        elif held > 0 and (ex[i] or held >= 5):               # strength exit or timeout
            held = 0
        elif held > 0:
            held += 1
        pos[i] = 1.0 if held > 0 else 0.0
    return run(pd.Series(pos, index=px.index), px, COST["SPY"], fill="close")

def spy_end_of_month(px: pd.DataFrame) -> dict:
    ym = px.index.to_period("M")
    day_in = ym.to_timestamp()                                # group by month
    g = pd.Series(np.arange(len(px)), index=px.index).groupby(ym)
    pos = pd.Series(0.0, index=px.index)
    order = g.cumcount()                                      # nth trading day of month
    size = g.transform("size")
    buy = order == (size - 5)                                 # 5th-last trading day
    sell_after = order == 2                                   # 3rd trading day
    state = 0.0; arr = np.zeros(len(px))
    b, s = buy.to_numpy(), sell_after.to_numpy()
    for i in range(len(px)):
        if b[i]:
            state = 1.0
        elif s[i]:
            state = 0.0
        arr[i] = state
    return run(pd.Series(arr, index=px.index), px, COST["SPY"], fill="close")

def ibs_strategy(px: pd.DataFrame, sym: str) -> dict:
    """IBS = (close-low)/(high-low); buy close when IBS<0.2, sell when IBS>0.8.
    [QuantifiedStrategies IBS backtests: SPY 0.8%/trade 78% win; QQQ 1.33%/trade 75%]"""
    rng = (px["high"] - px["low"])
    ibs = ((px["close"] - px["low"]) / rng.where(rng > 0)).fillna(0.5)
    pos = pd.Series(np.nan, index=px.index)
    pos[ibs < 0.2] = 1.0
    pos[ibs > 0.8] = 0.0
    return run(pos.ffill().fillna(0.0), px, COST[sym], fill="close")

def double7(px: pd.DataFrame, sym: str) -> dict:
    """Connors Double 7s: close > 200d MA and close = 7-day low close → buy close;
    sell close at a 7-day high close. [Connors & Alvarez book; QS: 82.5% win, PF 2.58]"""
    c, ma200 = px["close"], px["close"].rolling(200).mean()
    lo7, hi7 = c.rolling(7).min(), c.rolling(7).max()
    pos = pd.Series(np.nan, index=px.index)
    pos[(c > ma200) & (c <= lo7)] = 1.0
    pos[c >= hi7] = 0.0
    return run(pos.ffill().fillna(0.0), px, COST[sym], fill="close")

# --- SPY portfolio: the answer to "6 trades a year" ------------------------------------
SPY_LEGS = {
    "rsi2_200ma": spy_rsi2_200ma,
    "tt": spy_turnaround_tuesday,
    "eom": spy_end_of_month,
    "ibs": lambda px: ibs_strategy(px, "SPY"),
    "d7": lambda px: double7(px, "SPY"),
}

def spy_portfolio(px: pd.DataFrame, mode: str = "avg") -> dict:
    """Combine the five SPY legs. mode='avg': position = fraction of legs long
    (diversified, ≤100%); mode='union': fully long whenever any leg is long."""
    legs = pd.DataFrame({k: f(px)["pos"] for k, f in SPY_LEGS.items()})
    pos = legs.mean(axis=1) if mode == "avg" else (legs.max(axis=1) > 0).astype(float)
    return run(pos, px, COST["SPY"], fill="close")

def buy_hold(px: pd.DataFrame, sym: str) -> dict:
    return run(pd.Series(1.0, index=px.index), px, COST[sym], fill="close")

# --- report -----------------------------------------------------------------------------
def fmt(b: dict) -> str:
    if not b:
        return "<td colspan=7>—</td>"
    pf = "∞" if np.isinf(b["pf"]) else f"{b['pf']:.2f}"
    return (f"<td>{b['cagr']*100:+.1f}%</td><td>{b['sharpe']:.2f}</td>"
            f"<td>{b['maxdd']*100:.0f}%</td><td>{b['trades']}</td>"
            f"<td>{b['win']*100:.0f}%</td><td>{pf}</td><td>{b['exposure']*100:.0f}%</td>")

def main():
    h1, d1 = load_btc()
    spy, qqq = load_equity("SPY"), load_equity("QQQ")
    ppy_d, ppy_h = 252, 252 * 24

    rows = [
        metrics(btc_ma_20_100(d1), "BTC 20/100 MA cross (Grayscale)", 365),
        metrics(btc_d1h1_macd(h1, d1), "BTC D1H1 MACD (Quantpedia)", 365 * 24),
        metrics(buy_hold(d1, "BTC"), "BTC buy & hold", 365),
        metrics(rsi2_plain(spy, "SPY"), "SPY RSI-2 <10/>80 (QuantifiedStrategies)", ppy_d),
        metrics(spy_rsi2_200ma(spy), "SPY RSI-2 + 200MA (Connors book)", ppy_d),
        metrics(spy_turnaround_tuesday(spy), "SPY Turnaround Tuesday", ppy_d),
        metrics(spy_end_of_month(spy), "SPY End-of-month", ppy_d),
        metrics(buy_hold(spy, "SPY"), "SPY buy & hold", ppy_d),
        metrics(ibs_strategy(spy, "SPY"), "SPY IBS <0.2/>0.8", ppy_d),
        metrics(double7(spy, "SPY"), "SPY Double 7s (Connors book)", ppy_d),
        metrics(spy_portfolio(spy, "avg"), "SPY PORTFOLIO avg (5 legs)", ppy_d),
        metrics(spy_portfolio(spy, "union"), "SPY PORTFOLIO union (5 legs)", ppy_d),
        metrics(rsi2_plain(qqq, "QQQ"), "QQQ RSI-2 (robustness x-check)", ppy_d),
        metrics(ibs_strategy(qqq, "QQQ"), "QQQ IBS <0.2/>0.8", ppy_d),
        metrics(buy_hold(qqq, "QQQ"), "QQQ buy & hold", ppy_d),
    ]
    rows.sort(key=lambda r: -(r["oos"].get("sharpe") if r["oos"].get("sharpe") == r["oos"].get("sharpe") else -9))

    print(f"{'strategy':42} {'CAGR':>7} {'Sharpe':>6} {'MaxDD':>6} | {'OOS CAGR':>8} {'OOS Shp':>7}")
    for r in rows:
        f, o = r["full"], r["oos"]
        print(f"{r['label']:42} {f.get('cagr', np.nan)*100:6.1f}% {f.get('sharpe', np.nan):6.2f} "
              f"{f.get('maxdd', np.nan)*100:5.0f}% | {o.get('cagr', np.nan)*100:7.1f}% "
              f"{o.get('sharpe', np.nan):7.2f}")

    # equity curve figure (log scale, one panel per instrument)
    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    groups = {"BTC": axes[0], "SPY": axes[1], "QQQ": axes[2]}
    for r in rows:
        key = r["label"].split()[0]
        ax = groups.get(key)
        if ax is None:
            continue
        ax.plot(r["eq"].index, r["eq"], lw=1.2,
                label=r["label"].replace(key + " ", ""))
    for k, ax in groups.items():
        ax.set_yscale("log"); ax.set_title(k); ax.legend(fontsize=7); ax.grid(alpha=0.3)
        ax.axvline(OOS_START, color="red", ls="--", lw=0.8)
    fig.tight_layout()
    buf = io.BytesIO(); fig.savefig(buf, format="png", dpi=110)
    img = base64.b64encode(buf.getvalue()).decode()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = ROOT / f"llm_ict_bot/runs/reports/pivot_report_{ts}.html"
    head = ("<th>strategy</th><th colspan=7>full period (net of costs)</th>"
            "<th colspan=7>2025-01+ (post-publication)</th>")
    sub = ("<th></th>" + "<th>CAGR</th><th>Sharpe</th><th>MaxDD</th><th>trades</th>"
           "<th>win</th><th>PF</th><th>expo</th>" * 2)
    trs = "".join(f"<tr><td><b>{r['label']}</b></td>{fmt(r['full'])}{fmt(r['oos'])}</tr>"
                  for r in rows)
    out.write_text(f"""<html><head><title>Test 9 — evidence-backed pivot screen</title>
<style>body{{font-family:system-ui;margin:2em}}table{{border-collapse:collapse}}
td,th{{border:1px solid #ccc;padding:4px 8px;font-size:13px;text-align:right}}
td:first-child{{text-align:left}}</style></head><body>
<h1>Test 9 — evidence-backed pivot screen</h1>
<p>Published rules replicated <b>verbatim</b> (zero tuning by us), net of costs
(BTC 10 bps/side, equities 1 bp/side). Sorted by post-publication (2025+) Sharpe —
the red dashed line in the plots. SPY/QQQ dividend-adjusted. BTC: 2020-01→2026-06;
SPY: 1993→2026-07; QQQ: 1999→2026-07.</p>
<table><tr>{head}</tr><tr>{sub}</tr>{trs}</table>
<img src="data:image/png;base64,{img}" style="max-width:100%">
<p><i>Sources: Grayscale Research trend report; Quantpedia BTC multi-timeframe study;
QuantifiedStrategies RSI-2 / Turnaround Tuesday / end-of-month; Connors &amp; Alvarez.
The paywalled QQQ day-of-week strategy was not replicated — no guessed rules.</i></p>
</body></html>""")
    print(f"\nreport: {out}")

if __name__ == "__main__":
    main()
