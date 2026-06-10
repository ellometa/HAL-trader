# %% [markdown]
# ## 18 · Plots — equity, drawdown, trade markers

# %%
import base64
import matplotlib
import matplotlib.pyplot as plt
from jinja2 import Template

PLOT_PATHS = {}

def _save(fig, name: str) -> None:
    p = REPORT_DIR / f"{name}_{RUN_ID}.png"
    fig.savefig(p, dpi=110, bbox_inches="tight")
    PLOT_PATHS[name] = p

fig, ax = plt.subplots(figsize=(11, 4.5))
for key, res in RESULTS.items():
    ax.plot(res["curve"].index, res["curve"]["equity"], label=f"{res['label']}", lw=1.2)
ax.axhline(ACCOUNT_EQUITY, color="grey", lw=0.7, ls="--")
ax.set_title("Equity curves"); ax.set_ylabel("USD"); ax.legend(); ax.grid(alpha=0.3)
_save(fig, "equity"); plt.show()

fig, ax = plt.subplots(figsize=(11, 3.2))
for key, res in RESULTS.items():
    eq = res["curve"]["equity"]
    dd = 100 * (eq / eq.cummax() - 1)
    ax.plot(dd.index, dd, label=res["label"], lw=1.0)
ax.axhline(-100 * MAX_DRAWDOWN_HALT, color="red", lw=0.8, ls=":", label="halt level")
ax.set_title("Drawdown (%)"); ax.set_ylabel("%"); ax.legend(); ax.grid(alpha=0.3)
_save(fig, "drawdown"); plt.show()

# trade markers on price for the strategy with the most closed trades
_mk = max(RESULTS.values(),
          key=lambda r: len(r["trades"]) if "r_multiple" in getattr(r["trades"], "columns", []) else 0)
_tr = _mk["trades"]
_px_tf = (M_TF if DO_REAL else sm_tf)[DECISION_TF]
_w0, _w1 = _mk["curve"].index.min(), _mk["curve"].index.max()
_px = _px_tf[(_px_tf["close_time"] >= _w0) & (_px_tf["close_time"] <= _w1)]
fig, ax = plt.subplots(figsize=(11, 4.5))
ax.plot(_px.index, _px["close"], lw=0.6, color="black", alpha=0.7, label="EUR/USD 15m close")
if len(_tr) and "r_multiple" in _tr.columns:
    closed = _tr[_tr["r_multiple"].notna()]
    longs, shorts = closed[closed["direction"] == "long"], closed[closed["direction"] == "short"]
    ax.scatter(longs["entry_time"], longs["entry"], marker="^", color="green", s=42, label="long entry", zorder=3)
    ax.scatter(shorts["entry_time"], shorts["entry"], marker="v", color="red", s=42, label="short entry", zorder=3)
    win = closed[closed["pnl_usd"] > 0]; loss = closed[closed["pnl_usd"] <= 0]
    ax.scatter(win["exit_time"], win["exit_px"], marker="o", facecolors="none", edgecolors="green", s=36, label="exit (win)", zorder=3)
    ax.scatter(loss["exit_time"], loss["exit_px"], marker="x", color="red", s=36, label="exit (loss)", zorder=3)
ax.set_title(f"Trades on price — {_mk['label']}"); ax.legend(loc="best"); ax.grid(alpha=0.3)
_save(fig, "trades"); plt.show()

# %% [markdown]
# ## 19 · HTML report + CSV trade logs

# %%
def build_verdict() -> str:
    llm = compute_metrics(RESULTS["llm_ict"])
    rule = compute_metrics(RESULTS["rule_ict"])
    llm_real = DO_LLM and RESULTS["llm_ict"]["label"] == "llm_ict"
    if not llm_real:
        return ("This report was generated from the smoke-test stand-in (no real-data LLM run "
                "was executed), so no LLM-vs-rules conclusion can be drawn from it.")
    beat = (llm["total_return_pct"] > rule["total_return_pct"]) and \
           (np.isnan(rule.get("sharpe", np.nan)) or
            (not np.isnan(llm.get("sharpe", np.nan)) and llm["sharpe"] >= rule["sharpe"]))
    if beat:
        return (f"On the same out-of-sample window the LLM strategy returned "
                f"{llm['total_return_pct']:+.2f}% (Sharpe {llm['sharpe']}) vs the mechanical "
                f"rule baseline's {rule['total_return_pct']:+.2f}% (Sharpe {rule['sharpe']}) — "
                f"the LLM added value over its own deterministic inputs on this window. "
                f"Treat with caution: one window, one pair, paper costs.")
    return (f"**The LLM did not beat the mechanical rules.** On identical out-of-sample data the "
            f"LLM strategy returned {llm['total_return_pct']:+.2f}% (Sharpe {llm['sharpe']}, "
            f"avg R {llm['avg_R']}) vs {rule['total_return_pct']:+.2f}% (Sharpe {rule['sharpe']}, "
            f"avg R {rule['avg_R']}) for the rule-only ICT baseline. On this evidence the LLM "
            f"layer adds cost and latency without adding edge.")

VERDICT = build_verdict()

_REPORT_TMPL = Template("""<!doctype html><html><head><meta charset="utf-8">
<title>LLM + ICT paper bot — {{ run_id }}</title>
<style>
 body{font-family:-apple-system,Segoe UI,sans-serif;margin:2.2em;max-width:1100px;color:#1a1a1a}
 h1{font-size:1.5em}h2{font-size:1.15em;margin-top:1.6em;border-bottom:1px solid #ddd;padding-bottom:.2em}
 table{border-collapse:collapse;font-size:.85em;margin:.6em 0}
 th,td{border:1px solid #ccc;padding:.3em .6em;text-align:right}
 th{background:#f2f2f2}td:first-child,th:first-child{text-align:left}
 .verdict{background:#f8f4e8;border-left:4px solid #c9a227;padding:.9em 1.1em;margin:1em 0;font-size:.95em}
 .meta{color:#555;font-size:.85em}img{max-width:100%;border:1px solid #eee;margin:.4em 0}
</style></head><body>
<h1>LLM + ICT hybrid paper-trading bot — EUR/USD</h1>
<p class="meta">run {{ run_id }} · mode {{ mode }} · window {{ start }} → {{ end }} ·
model {{ model }} (temp 0, seed {{ seed }}) · decision TF {{ dec_tf }} · NY session entries only ·
spread {{ spread }} pip · risk {{ risk }}%/trade · RR 1:{{ rr }} · confidence gate ≥ {{ gate }}</p>
<div class="verdict"><b>Robustness verdict.</b> {{ verdict }}</div>
<h2>Summary — LLM vs rule-only vs buy-and-hold</h2>
{{ summary_table }}
<h2>Decision funnel & skip counts</h2>
{{ counters_table }}
<h2>Circuit-breaker halts</h2>
{% if halts %}<table><tr><th>strategy</th><th>time</th><th>kind</th><th>detail</th></tr>
{% for h in halts %}<tr><td>{{ h.strategy }}</td><td>{{ h.time }}</td><td>{{ h.kind }}</td><td>{{ h.detail }}</td></tr>{% endfor %}
</table>{% else %}<p>None tripped.</p>{% endif %}
{% if folds_table %}<h2>Walk-forward folds (anchored expanding window)</h2>{{ folds_table }}{% endif %}
<h2>Equity curves</h2><img src="data:image/png;base64,{{ img_equity }}">
<h2>Drawdown</h2><img src="data:image/png;base64,{{ img_drawdown }}">
<h2>Trades on price</h2><img src="data:image/png;base64,{{ img_trades }}">
<h2>Data quality</h2>
<p class="meta">{{ data_note }}</p>
</body></html>""")

def _b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()

counters_df = pd.DataFrame({res["label"]: res["counters"] for res in RESULTS.values()
                            if res["counters"]}).fillna(0).astype(int)
halts_flat = [{"strategy": res["label"], **h} for res in RESULTS.values() for h in res["halts"]]
folds_html = RESULTS["llm_ict"]["folds"].to_html(index=False) \
    if "folds" in RESULTS["llm_ict"] else None

html = _REPORT_TMPL.render(
    run_id=RUN_ID, mode=RUN_MODE,
    start=str(RESULTS["llm_ict"]["curve"].index.min().date()),
    end=str(RESULTS["llm_ict"]["curve"].index.max().date()),
    model=LLM_MODEL_ID, seed=ACTIVE_LLM_PARAMS.get("seed"), dec_tf=DECISION_TF,
    spread=SPREAD_PIPS, risk=int(RISK_PCT * 100), rr=int(RR_TARGET), gate=CONF_THRESHOLD,
    verdict=VERDICT,
    summary_table=summary_df.to_html(),
    counters_table=counters_df.to_html(),
    halts=halts_flat, folds_table=folds_html,
    img_equity=_b64(PLOT_PATHS["equity"]), img_drawdown=_b64(PLOT_PATHS["drawdown"]),
    img_trades=_b64(PLOT_PATHS["trades"]),
    data_note=(f"source: {'real parquet store' if HAVE_REAL_DATA else 'synthetic fallback'} · "
               f"{len(clean_1m):,} clean 1m bars · {n_weekend:,} weekend bars dropped · "
               f"{len(holiday_days)} holiday days dropped · {len(gaps_df):,} intra-session "
               f"gaps flagged (never filled) · {len(thin_days)} thin days flagged"))

report_path = REPORT_DIR / f"report_{RUN_ID}.html"
report_path.write_text(html)

csv_paths = []
for res in RESULTS.values():
    if len(res["trades"]):
        p = REPORT_DIR / f"trades_{res['label']}_{RUN_ID}.csv"
        res["trades"].to_csv(p, index=False)
        csv_paths.append(p)

print(f"report  → {report_path}")
for p in csv_paths:
    print(f"trades  → {p}")

# %% [markdown]
# ## 20 · Final summary

# %%
print("=" * 78)
print("LLM + ICT HYBRID PAPER BOT — RUN SUMMARY")
print("=" * 78)
print(f"mode {RUN_MODE} | window {BACKTEST_START.date()} → {BACKTEST_END.date()} | "
      f"model {LLM_MODEL_ID} | smoke {'PASS' if cnt['fills'] >= 1 else '??'}")
print("-" * 78)
print(summary_df.to_string())
print("-" * 78)
for res in RESULTS.values():
    c = res["counters"]
    if c:
        print(f"{res['label']:>10}: consults {c.get('llm_consults', 0):,} | "
              f"no-trade {c.get('no_trade', 0):,} | gated {c.get('gated', 0):,} | "
              f"rejected {c.get('validator_rejected', 0):,} | fills {c.get('fills', 0):,} | "
              f"breaker blocks {c.get('breaker_day_blocks', 0) + c.get('breaker_pause_blocks', 0) + c.get('breaker_halt_blocks', 0):,}")
print("-" * 78)
print("VERDICT:", re.sub(r"\*\*", "", VERDICT))
print(f"artifacts: {report_path.name}, JSONL audit logs in {LOG_DIR}/, "
      f"LLM cache in {CACHE_DIR}/")
print("=" * 78)
