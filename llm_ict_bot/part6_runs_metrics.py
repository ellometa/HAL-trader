# %% [markdown]
# ## 16 · Real-data runs
#
# Guards: the LLM walk-forward needs the real Parquet store **and** a ready LLM
# provider (Gemini key or running Ollama, per `LLM_PROVIDER`); the rule baseline and
# buy-and-hold need only the store. If something is missing the notebook still
# completes — the reporting cells fall back to the smoke results so every artifact
# below always renders.

# %%
def ollama_alive() -> bool:
    try:
        r = requests.get(OLLAMA_URL.replace("/api/generate", "/api/tags"), timeout=3)
        return r.ok and any(m.get("name", "").startswith(OLLAMA_MODEL.split(":")[0])
                            for m in r.json().get("models", []))
    except requests.RequestException:
        return False


def llm_ready() -> bool:
    """Provider-aware healthcheck: one real (cheap) generation must succeed."""
    if LLM_PROVIDER == "ollama":
        return ollama_alive()
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY is not set — export it (aistudio.google.com/apikey) "
              "or set LLM_PROVIDER='ollama' in the config cell")
        return False
    try:
        gemini_generate('Respond with exactly this JSON: {"ok": true}', system="echo test")
        return True
    except Exception as e:
        print(f"Gemini healthcheck failed: {e!r}")
        return False

SKIP_LLM = bool(os.environ.get("BOT_SKIP_LLM"))   # rule-baseline-only run
LLM_READY = (not SKIP_LLM) and llm_ready()
DO_REAL = HAVE_REAL_DATA and not RUN_SMOKE_ONLY
DO_LLM = DO_REAL and LLM_READY
print(f"real data: {HAVE_REAL_DATA} | {LLM_MODEL_ID} ready: {LLM_READY} "
      f"| real runs: {DO_REAL} | LLM run: {DO_LLM}")

# %%
RESULTS: dict[str, dict] = {}

if DO_REAL:
    _t0 = _time.time()
    INTRADAY_START = BACKTEST_START - pd.Timedelta(days=60)   # detector warm-up buffer
    M_TF, M_DET = prepare_market(clean_1m, intraday_start=INTRADAY_START)
    _dec_ct = pd.DatetimeIndex(M_TF[DECISION_TF]["close_time"])
    _slots = int(((_dec_ct > BACKTEST_START) & (_dec_ct <= BACKTEST_END)
                  & in_ny_session(_dec_ct)).sum())
    _per_call = max(1.5, GEMINI_MIN_INTERVAL_S) if LLM_PROVIDER == "gemini" else 40
    print(f"market prepared in {_time.time() - _t0:.0f}s — {_slots:,} NY-session decision "
          f"slots in window (≈{_slots * _per_call / 3600:.1f}h of LLM compute at "
          f"~{_per_call:.0f}s/call via {LLM_MODEL_ID}, fewer while a position is open)")

# %% [markdown]
# ### Rule-only ICT baseline + buy-and-hold (deterministic, fast)

# %%
if DO_REAL and os.environ.get("BOT_COMPARE_STRATS"):
    # Three named ICT strategies compared head-to-head (see STRATEGY_COMPARISON_SPEC.md).
    # SLIPSTREAM is keyed "rule_ict" so the existing report wiring works unchanged.
    print("3-strategy ICT comparison — SLIPSTREAM / MIDNIGHT RAID / BLOODHOUND:")
    RESULTS["rule_ict"] = run_walk_forward(clean_1m, M_TF, M_DET,
        make_breakout_decide_fn(M_DET), "SLIPSTREAM")
    RESULTS["midnight_raid"] = run_walk_forward(clean_1m, M_TF, M_DET,
        make_judas_decide_fn(M_DET, M_TF), "MIDNIGHT RAID")
    RESULTS["bloodhound"] = run_walk_forward(clean_1m, M_TF, M_DET,
        make_breakout_liq_decide_fn(M_DET, M_TF), "BLOODHOUND")
    for _k in ("rule_ict", "midnight_raid", "bloodhound"):
        _r = RESULTS[_k]
        print(f"  {_r['label']:14} trades {len(_r['trades']):>3}  "
              f"final ${_r['final_equity']:,.0f}")
    RESULTS["buy_hold"] = buy_and_hold(M_TF, BACKTEST_START, BACKTEST_END)
    print(f"buy-and-hold final equity: ${RESULTS['buy_hold']['final_equity']:,.0f}")
elif DO_REAL:
    # BOT_STRATEGY=breakout_bias swaps the shipped reversal rule for the improved
    # breakout + 1h-HTF-bias strategy (Test 5 winner). Kept under the "rule_ict" key
    # so all downstream reporting works unchanged; the label reflects which ran.
    _strat = os.environ.get("BOT_STRATEGY", "reversal")
    if _strat == "breakout_bias":
        _decide, _label = make_breakout_decide_fn(M_DET), "ict_breakout_bias"
    else:
        _decide, _label = make_rule_decide_fn(M_DET), "rule_ict"
    print(f"rule strategy: {_label} (RR target {RR_TARGET:g}):")
    RESULTS["rule_ict"] = run_walk_forward(clean_1m, M_TF, M_DET, _decide, _label)
    RESULTS["buy_hold"] = buy_and_hold(M_TF, BACKTEST_START, BACKTEST_END)
    print(f"buy-and-hold final equity: ${RESULTS['buy_hold']['final_equity']:,.0f}")
else:
    print("real data unavailable / smoke-only — reporting will use smoke results")
    RESULTS["rule_ict"] = sm_rule
    RESULTS["buy_hold"] = buy_and_hold(sm_tf, sm_start, sm_end)

# %% [markdown]
# ### LLM walk-forward (the headline run)
#
# Every NY-session 15m close while flat → context → the configured model
# (`LLM_MODEL_ID`) → validated plan. Responses are cached by context hash, so
# re-running the notebook replays from disk — widening the window later reuses
# every completed call. Audit trail: `runs/logs/llm_calls_<run-id>.jsonl`.

# %%
if DO_LLM:
    llm_cache = LLMCache(CACHE_DIR / "real")
    llm_logger = JsonlLogger(LOG_DIR / f"llm_calls_{RUN_ID}.jsonl")
    print(f"LLM walk-forward ({LLM_MODEL_ID}, temp 0, seed {ACTIVE_LLM_PARAMS.get('seed')}):")
    _t0 = _time.time()
    RESULTS["llm_ict"] = run_walk_forward(
        clean_1m, M_TF, M_DET, make_llm_decide_fn(llm_cache, llm_logger), "llm_ict",
        max_decisions=MAX_LLM_DECISIONS)
    _c = RESULTS["llm_ict"]["counters"]
    print(f"done in {(_time.time() - _t0) / 3600:.2f}h — consults {_c['llm_consults']:,} "
          f"(cache {_c['cache_hits']:,}), orders {_c['orders']}, gated {_c['gated']}, "
          f"rejected {_c['validator_rejected']}, llm errors {_c['llm_errors']}")
elif DO_REAL:
    print("LLM walk-forward skipped "
          + ("(BOT_SKIP_LLM set — rule baseline + buy-and-hold only)." if SKIP_LLM else
             f"({LLM_MODEL_ID} not ready. For Gemini: export GEMINI_API_KEY. "
             "For Ollama: start `ollama serve` and pull the model)."))
else:
    RESULTS["llm_ict"] = sm_res          # smoke stand-in so reporting always renders

# %% [markdown]
# ### Live-forward mode
#
# The same engine driven in strict arrival order over the most recent days — a paper
# replay of real-time decisioning (`verbose=True` streams every order, fill and exit
# as it would print live; no internet at runtime, so "live" means replaying the newest
# bars in the store). A short rule-strategy demo always runs; set
# `RUN_MODE = "live_forward"` to run the LLM live-forward over `LIVE_FORWARD_DAYS`.

# %%
if DO_REAL:
    lf_end = BACKTEST_END
    lf_start = lf_end - pd.Timedelta(days=3)
    print(f"live-forward demo (rule strategy, {lf_start.date()} → {lf_end.date()}):")
    lf_demo = run_engine(clean_1m, M_TF, M_DET, make_rule_decide_fn(M_DET),
                         lf_start, lf_end, "rule_live_demo", verbose=True)
    print(f"demo finished — {len(lf_demo['trades'])} trade(s), "
          f"final equity ${lf_demo['final_equity']:,.0f}")
    if RUN_MODE == "live_forward" and LLM_READY:
        lf_start = lf_end - pd.Timedelta(days=LIVE_FORWARD_DAYS)
        print(f"\nLIVE-FORWARD LLM run ({lf_start.date()} → {lf_end.date()}):")
        RESULTS["llm_live"] = run_engine(
            clean_1m, M_TF, M_DET,
            make_llm_decide_fn(LLMCache(CACHE_DIR / "real"),
                               JsonlLogger(LOG_DIR / f"llm_live_{RUN_ID}.jsonl")),
            lf_start, lf_end, "llm_live", verbose=True)

# %% [markdown]
# ## 17 · Metrics
#
# Win rate, average R, Sharpe, Sortino, max drawdown, profit factor, expectancy,
# trade count and average hold time — computed identically for every strategy.
# Sharpe/Sortino use daily equity returns annualized by √252.

# %%
def compute_metrics(res: dict, equity0: float = ACCOUNT_EQUITY) -> dict:
    tr, eq = res["trades"], res["curve"]["equity"]
    out = {"strategy": res["label"],
           "final_equity": round(res["final_equity"], 2),
           "total_return_pct": round(100 * (res["final_equity"] / equity0 - 1), 2)}
    closed = tr[tr["r_multiple"].notna()] if "r_multiple" in tr.columns and len(tr) else pd.DataFrame()
    if len(closed):
        pnl, r = closed["pnl_usd"], closed["r_multiple"]
        wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
        hold = (pd.to_datetime(closed["exit_time"]) - pd.to_datetime(closed["entry_time"]))
        out.update(trades=len(closed),
                   win_rate_pct=round(100 * (pnl > 0).mean(), 1),
                   avg_R=round(r.mean(), 3),
                   expectancy_usd=round(pnl.mean(), 2),
                   profit_factor=round(wins.sum() / abs(losses.sum()), 2) if len(losses) and losses.sum() != 0 else float("inf"),
                   avg_hold_hours=round(hold.dt.total_seconds().mean() / 3600, 2))
    else:
        out.update(trades=0, win_rate_pct=np.nan, avg_R=np.nan, expectancy_usd=np.nan,
                   profit_factor=np.nan, avg_hold_hours=np.nan)
    daily = eq.resample("1D").last().dropna()
    rets = daily.pct_change().dropna()
    if len(rets) > 2 and rets.std() > 0:
        out["sharpe"] = round(rets.mean() / rets.std() * np.sqrt(252), 2)
        downside = rets[rets < 0].std()
        out["sortino"] = round(rets.mean() / downside * np.sqrt(252), 2) \
            if downside and downside > 0 else float("inf")
    else:
        out["sharpe"] = out["sortino"] = np.nan
    out["max_drawdown_pct"] = round(100 * (eq / eq.cummax() - 1).min(), 2)
    return out


summary_df = pd.DataFrame([compute_metrics(r) for r in RESULTS.values()]).set_index("strategy")
summary_df
