# %% [markdown]
# ## 13 · Strategies
#
# Three deciders share the engine's single code path:
#
# 1. **LLM strategy** — context → Ollama → validated `TradePlan` (the system under test).
# 2. **Rule-only ICT baseline** — the *primary comparison*: a mechanical version of the
#    exact confluence the LLM is asked to judge (sweep → structure shift → premium/
#    discount filter), isolating whether the LLM adds value over its own inputs.
# 3. **Mock LLM** — deterministic scripted responses (including malformed JSON and an
#    R:R violation) that exercise the parser, validator, retry and confidence-gate
#    paths in the smoke test without an Ollama server.
#
# Plus a **buy-and-hold** reference (flat-ish for FX; included for completeness).

# %%
def make_llm_decide_fn(cache: LLMCache, logger: JsonlLogger,
                       generate_fn: Optional[Callable[[str], str]] = None) -> Callable:
    def decide(ctx_provider, price, t):
        return llm_decide(ctx_provider(), price, cache, logger,
                          generate_fn=generate_fn or DEFAULT_GENERATE, decision_time=t)
    return decide


# --- rule-only ICT baseline ---------------------------------------------------------
RULE_SWEEP_WINDOW = pd.Timedelta("3h")   # sweep must be at most this old (12 × 15m bars)
RULE_MIN_STOP_PIPS = 5.0
RULE_SL_BUFFER_PIPS = 2.0

def make_rule_decide_fn(det_store: dict) -> Callable:
    """Mechanical ICT confluence, mirroring the LLM's A+ template:
    recent 15m liquidity sweep → 15m structure shift in the sweep's reversal direction
    confirmed after the sweep → 1h premium/discount filter. SL beyond the sweep wick,
    TP at exactly 1:RR_TARGET. No confidence notion (always passes the gate)."""
    d15, d1h = det_store["15min"], det_store["1h"]

    def decide(ctx_provider, price, t):
        none = {"plan": TradePlan("none"), "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
        sweeps = visible(d15["sweeps"], t, since=t - RULE_SWEEP_WINDOW)
        if sweeps.empty:
            return none
        s = sweeps.iloc[-1]
        want = "long" if s["side"] == "sellside" else "short"
        ev = visible(d15["structure"], t)
        ev_after = ev[ev["time"] > s["time"]]
        shift_kinds = ("CHoCH_up", "BOS_up") if want == "long" else ("CHoCH_down", "BOS_down")
        if ev_after.empty or not ev_after["kind"].isin(shift_kinds).any():
            return none
        pdd = premium_discount(visible(d1h["swings"], t, since=context_window_start(t)), price)
        if pdd is None or (want == "long" and pdd["zone"] != "discount") \
                or (want == "short" and pdd["zone"] != "premium"):
            return none
        buf = RULE_SL_BUFFER_PIPS * PIP
        sl = s["wick_extreme"] - buf if want == "long" else s["wick_extreme"] + buf
        risk = (price - sl) if want == "long" else (sl - price)
        if risk < RULE_MIN_STOP_PIPS * PIP:
            return none
        tp = price + RR_TARGET * risk if want == "long" else price - RR_TARGET * risk
        plan = TradePlan(want, entry=price, stop_loss=sl, take_profit=tp,
                         confidence=100, reasoning="rule: sweep→shift→P/D confluence")
        ok, reason = validate_plan(plan, price)
        if not ok:
            return {**none, "status": "validator_rejected", "reject_reason": reason}
        return {"plan": plan, "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
    return decide


# --- improved ICT: breakout/continuation + higher-timeframe bias --------------------
# The best out-of-sample config from the tune_rules.py study (Test 5 in
# RESEARCH_SUMMARY): trade WITH a recent 15m displacement, but only when it agrees with
# the 1h trend (proper-ICT HTF bias), confirmed by a same-direction structure shift, in
# the favorable 4h premium/discount zone; stop beyond the last opposing 15m swing.
# Engine-side port of tune_rules.py so it can produce a full notebook report; uses
# RR_TARGET (set BOT_RR=1.5 to match the validated config).
BRK_SIGNAL_WINDOW = pd.Timedelta("2h")
BRK_MIN_STOP_PIPS = 8.0

def make_breakout_decide_fn(det_store: dict, htf_bias_tf: str = "1h",
                            pd_tf: str = "4h") -> Callable:
    d15, dbias, dpd = det_store["15min"], det_store[htf_bias_tf], det_store[pd_tf]

    def decide(ctx_provider, price, t):
        none = {"plan": TradePlan("none"), "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
        disp = visible(d15["displacement"], t, since=t - BRK_SIGNAL_WINDOW)
        if disp.empty:
            return none
        want = "long" if disp.iloc[-1]["direction"] == "bullish" else "short"
        d_time = disp.iloc[-1]["time"]
        # HTF bias: only trade with the 1h trend
        bias = visible(dbias["structure"], t)
        if bias.empty:
            return none
        trend = bias.iloc[-1]["trend_after"]
        if (want == "long" and trend != 1) or (want == "short" and trend != -1):
            return none
        # same-direction structure shift after the displacement
        ev_after = visible(d15["structure"], t)
        ev_after = ev_after[ev_after["time"] > d_time]
        shift_kinds = ("CHoCH_up", "BOS_up") if want == "long" else ("CHoCH_down", "BOS_down")
        if ev_after.empty or not ev_after["kind"].isin(shift_kinds).any():
            return none
        pdd = premium_discount(visible(dpd["swings"], t, since=context_window_start(t)), price)
        if pdd is None or (want == "long" and pdd["zone"] != "discount") \
                or (want == "short" and pdd["zone"] != "premium"):
            return none
        # stop beyond the last opposing 15m swing
        sw = visible(d15["swings"], t, since=context_window_start(t))
        sw = sw[sw["kind"] == ("low" if want == "long" else "high")]
        if sw.empty:
            return none
        ref = sw.iloc[-1]["level"]
        buf = RULE_SL_BUFFER_PIPS * PIP
        sl = ref - buf if want == "long" else ref + buf
        risk = (price - sl) if want == "long" else (sl - price)
        if risk < BRK_MIN_STOP_PIPS * PIP:
            return none
        tp = price + RR_TARGET * risk if want == "long" else price - RR_TARGET * risk
        plan = TradePlan(want, entry=price, stop_loss=sl, take_profit=tp,
                         confidence=100, reasoning="breakout + 1h HTF bias + P/D")
        ok, reason = validate_plan(plan, price)
        if not ok:
            return {**none, "status": "validator_rejected", "reject_reason": reason}
        return {"plan": plan, "status": "ok", "cache_hit": False,
                "attempts": 1, "reject_reason": ""}
    return decide


# --- deterministic mock LLM for the smoke test --------------------------------------
class MockLLM:
    """Scripted generate_fn. Cycles through: valid long / none / low-confidence long
    (gated) / malformed text (forces a retry) / valid short / R:R violation."""
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        m = re.search(r"CURRENT PRICE : (\d+\.\d+)", prompt)
        p = float(m.group(1)) if m else 1.1000
        rk = 0.0010                       # stop distance
        tgt = round(RR_TARGET * rk, 5)    # on-target TP distance (tracks BOT_RR override)
        bad = round((RR_TARGET + 1.0) * rk, 5)   # off-target distance → must fail validator
        i = self.calls % 6
        self.calls += 1
        if i == 0:
            return json.dumps({"direction": "long", "entry": p, "stop_loss": p - rk,
                               "take_profit": p + tgt, "confidence": 85,
                               "reasoning": "mock A+ long"})
        if i == 1:
            return json.dumps({"direction": "none", "entry": 0, "stop_loss": 0,
                               "take_profit": 0, "confidence": 10, "reasoning": "mock no-trade"})
        if i == 2:
            return json.dumps({"direction": "long", "entry": p, "stop_loss": p - rk,
                               "take_profit": p + tgt, "confidence": 40,
                               "reasoning": "mock low-confidence (should be gated)"})
        if i == 3:
            return "I think we should buy here because momentum looks good."   # parse failure
        if i == 4:
            return json.dumps({"direction": "short", "entry": p, "stop_loss": p + rk,
                               "take_profit": p - tgt, "confidence": 90,
                               "reasoning": "mock A+ short"})
        return json.dumps({"direction": "long", "entry": p, "stop_loss": p - rk,
                           "take_profit": p + bad, "confidence": 95,
                           "reasoning": "mock wrong-R:R (should be rejected)"})


# --- buy-and-hold reference ----------------------------------------------------------
def buy_and_hold(tf_store: dict, start: pd.Timestamp, end: pd.Timestamp,
                 equity0: float = ACCOUNT_EQUITY) -> dict:
    """Hold long EUR/USD at 1× equity notional across the window (mark-to-market on
    15m closes; one round-trip spread charged). FX has no drift entitlement — this is
    a reference line, not a strategy."""
    dec = tf_store[DECISION_TF]
    sel = dec[(dec["close_time"] > start) & (dec["close_time"] <= end)]
    entry = float(sel["open"].iloc[0])
    units = equity0 / entry
    cost = SPREAD_PIPS * PIP * units
    eq = equity0 + (sel["close"] - entry) * units - cost
    curve = pd.DataFrame({"equity": eq.values}, index=pd.DatetimeIndex(sel["close_time"]))
    curve.index.name = "time"
    trades = pd.DataFrame([{
        "strategy": "buy_hold", "direction": "long", "entry_time": sel.index[0],
        "entry": entry, "stop_loss": np.nan, "take_profit": np.nan, "units": units,
        "risk_usd": np.nan, "confidence": np.nan, "reasoning": "hold",
        "decided_at": sel.index[0], "exit_time": sel.index[-1],
        "exit_px": float(sel["close"].iloc[-1]), "reason": "end_of_run",
        "pnl_usd": float(eq.iloc[-1]) - equity0, "r_multiple": np.nan,
        "equity_after": float(eq.iloc[-1])}])
    return {"label": "buy_hold", "trades": trades, "curve": curve, "counters": {},
            "halts": [], "final_equity": float(eq.iloc[-1]),
            "final_peak": float(eq.max()), "halted": False}

print("strategies ready")

# %% [markdown]
# ## 14 · Walk-forward backtest — anchored expanding window
#
# **The split.** The test period is cut into calendar-month folds. For fold *k*, the
# *anchor* window is everything from the start of the data store up to the fold's first
# bar — detectors, structure state and LLM context for any decision inside fold *k* may
# draw only on that (always-growing) history; the fold itself is evaluated strictly
# out-of-sample, then the window rolls forward one month.
#
# **Honest caveat:** nothing here *fits* parameters — the LLM is frozen and the rules
# are fixed — so the anchored walk-forward reduces to sequential out-of-sample
# evaluation. We keep the fold structure anyway: it mirrors the standard protocol,
# gives per-fold stability diagnostics, and equity/breaker state (including the
# peak-drawdown high-water mark) carries across folds like one continuous account.
# Point-in-time discipline inside each fold is enforced by the context builder's
# `confirmed_time`/`close_time` filters and runtime leakage guard, as documented above.

# %%
def month_folds(start: pd.Timestamp, end: pd.Timestamp) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    edges = [start] + list(pd.date_range(start, end, freq=WF_FOLD_FREQ, inclusive="right")) + [end]
    edges = sorted(set(edges))
    return [(a, b) for a, b in zip(edges[:-1], edges[1:]) if a < b]


def run_walk_forward(df_1m: pd.DataFrame, tf_store: dict, det_store: dict,
                     decide_fn: Callable, label: str,
                     start: pd.Timestamp = BACKTEST_START, end: pd.Timestamp = BACKTEST_END,
                     max_decisions: Optional[int] = None, quiet: bool = False) -> dict:
    folds = month_folds(start, end)
    carry, peak = ACCOUNT_EQUITY, ACCOUNT_EQUITY
    parts, fold_rows = [], []
    remaining = max_decisions
    # BOT_PROGRESS=1 shows a tqdm bar and writes a flushed per-fold line to
    # BOT_PROGRESS_FILE (default runs/logs/progress.txt) — readable live during a run
    # that nbconvert would otherwise buffer until the cell finishes.
    _prog = bool(os.environ.get("BOT_PROGRESS"))
    _pfile = os.environ.get("BOT_PROGRESS_FILE", str(LOG_DIR / "progress.txt"))
    _iter = enumerate(folds, 1)
    if _prog:
        import sys
        from tqdm import tqdm
        open(_pfile, "w").close()                       # reset for this run
        _iter = tqdm(_iter, total=len(folds), desc=f"{label}", file=sys.stdout, ncols=80)
    for k, (s, e) in _iter:
        r = run_engine(df_1m, tf_store, det_store, decide_fn, s, e, label,
                       equity0=carry, peak0=peak, max_decisions=remaining)
        parts.append(r)
        fold_rows.append({"fold": k, "test_start": s.date(), "test_end": e.date(),
                          "anchor": f"{df_1m.index[0].date()} → {s.date()}",
                          "trades": len(r["trades"]), "end_equity": round(r["final_equity"], 2),
                          "halted": r["halted"]})
        if remaining is not None:
            remaining = max(0, remaining - r["counters"]["llm_consults"])
        carry, peak = r["final_equity"], r["final_peak"]
        if _prog:
            with open(_pfile, "a") as _pf:
                _pf.write(f"fold {k}/{len(folds)} {100*k//len(folds)}%  {e.date()}  "
                          f"trades {len(r['trades'])}  equity ${carry:,.0f}"
                          + ("  HALTED" if r["halted"] else "") + "\n")
        elif not quiet:
            print(f"  fold {k:>2}/{len(folds)}  {s.date()} → {e.date()}  "
                  f"trades {len(r['trades']):>3}  equity ${carry:,.0f}"
                  + ("  [HALTED]" if r["halted"] else ""))
        if r["halted"]:
            print(f"  max-drawdown halt tripped — run stops here, per circuit-breaker policy")
            break
    trades = pd.concat([p["trades"] for p in parts if len(p["trades"])], ignore_index=True) \
        if any(len(p["trades"]) for p in parts) else pd.DataFrame()
    curve = pd.concat([p["curve"] for p in parts])
    counters = {}
    for p in parts:
        for k2, v in p["counters"].items():
            counters[k2] = counters.get(k2, 0) + v
    halts = [h for p in parts for h in p["halts"]]
    return {"label": label, "trades": trades, "curve": curve, "counters": counters,
            "halts": halts, "final_equity": carry, "final_peak": peak,
            "halted": parts[-1]["halted"], "folds": pd.DataFrame(fold_rows)}

print(f"walk-forward ready — folds: {len(month_folds(BACKTEST_START, BACKTEST_END))} "
      f"({BACKTEST_START.date()} → {BACKTEST_END.date()})")

# %% [markdown]
# ## 15 · Smoke test — synthetic data, mock LLM, full pipeline
#
# Runs unconditionally and fast (no Ollama, no Parquet store needed): synthetic 1m
# data → clean → resample → detectors → context builder → mock LLM → validator →
# cache/log → simulator → assertions on every code path (fills, confidence gate,
# retry, validator rejection, cache replay).

# %%
_t0 = _time.time()
smoke_raw = make_synthetic_1m(days=15, seed=11)
smoke_1m, _ = drop_weekends(smoke_raw)
sm_tf, sm_det = prepare_market(smoke_1m)
# The assertions below exercise cold-start paths (retry, gate, parse); a smoke cache
# left over from a previous run would replay everything and mask them — start cold.
shutil.rmtree(CACHE_DIR / "smoke", ignore_errors=True)
sm_cache = LLMCache(CACHE_DIR / "smoke")
sm_logger = JsonlLogger(LOG_DIR / f"smoke_{RUN_ID}.jsonl")
sm_decide = make_llm_decide_fn(sm_cache, sm_logger, generate_fn=MockLLM())

sm_start, sm_end = smoke_1m.index[0], smoke_1m.index[-1] + pd.Timedelta("1min")
sm_res = run_engine(smoke_1m, sm_tf, sm_det, sm_decide, sm_start, sm_end, "mock_llm")

cnt = sm_res["counters"]
assert cnt["fills"] >= 1, "smoke: expected at least one filled trade"
assert cnt["gated"] >= 1, "smoke: confidence gate never triggered"
assert cnt["retries"] >= 1, "smoke: retry path never exercised"
assert cnt["no_trade"] >= 1, "smoke: 'none' path never exercised"
assert len(sm_res["curve"]) > 100 and np.isfinite(sm_res["final_equity"])
assert not sm_res["trades"].empty and {"r_multiple", "reason"} <= set(sm_res["trades"].columns)

# cache replay: identical contexts must be answered from cache, zero mock calls
sm_decide2 = make_llm_decide_fn(sm_cache, sm_logger, generate_fn=MockLLM())
sm_res2 = run_engine(smoke_1m, sm_tf, sm_det, sm_decide2, sm_start, sm_end, "mock_llm_replay")
assert sm_res2["counters"]["cache_hits"] == sm_res2["counters"]["llm_consults"], \
    "smoke: cache replay should answer every consult"

# rule baseline runs end-to-end on synthetic too (trade count may legitimately be 0)
sm_rule = run_engine(smoke_1m, sm_tf, sm_det, make_rule_decide_fn(sm_det),
                     sm_start, sm_end, "rule_smoke")

print(f"SMOKE OK in {_time.time() - _t0:.1f}s — consults {cnt['llm_consults']}, "
      f"fills {cnt['fills']}, gated {cnt['gated']}, retries {cnt['retries']}, "
      f"rejected {cnt['validator_rejected']}, cache replay clean, "
      f"rule-baseline trades {len(sm_rule['trades'])}")
