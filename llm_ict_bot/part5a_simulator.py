# %% [markdown]
# ## 12 · Execution simulator (paper)
#
# Streams **1-minute** bars in strict order — intrabar SL/TP resolution happens at 1m
# granularity even though decisions fire on 15m closes.
#
# - **Fills:** a plan decided at close `t` becomes a market order filled at the **next
#   1m bar's open**. The simulator never fills on the close the LLM just saw.
# - **Costs:** spread only — a flat `SPREAD_PIPS` deduction per round trip (prices are
#   mid; triggers evaluate on mid). No slippage, no commission, per spec.
# - **Conservatism:** if a 1m bar touches both SL and TP, the **SL is assumed to hit
#   first** (pessimistic). If the fill-bar open already gaps beyond SL or TP, the order
#   is cancelled and logged rather than filled into a degenerate position.
# - **Sizing:** `units = (equity × RISK_PCT) / |fill − SL|` — exactly 1% of current
#   equity at risk; confidence never scales size (it is a gate only).
# - **One position at a time**; positions may carry outside the session until SL/TP.
# - **Circuit breakers** (all logged): daily loss ≥ 3% of start-of-day equity → no new
#   entries until the next NY day; ≥ 5 consecutive losses → paused for the rest of the
#   day (counter carries overnight); peak-to-trough drawdown ≥ 15% → the run halts
#   permanently. Breaker-blocked decision slots skip the LLM call entirely.

# %%
def _session_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

_SESS_LO = _session_minutes(NY_SESSION_START)
_SESS_HI = _session_minutes(NY_SESSION_END)

def in_ny_session(times: pd.DatetimeIndex) -> np.ndarray:
    """True where a (tz-naive UTC) timestamp falls inside the NY entry session."""
    ny = times.tz_localize("UTC").tz_convert(TZ_NY)
    hm = ny.hour * 60 + ny.minute
    return np.asarray((ny.dayofweek < 5) & (hm >= _SESS_LO) & (hm < _SESS_HI))


def prepare_market(df_1m: pd.DataFrame, intraday_start: Optional[pd.Timestamp] = None
                   ) -> tuple[dict, dict]:
    """Resample once per TF and run every detector once. HTF frames (1h/4h/1d) keep
    full history (structure state benefits from a long warm-up); the heavy intraday
    frames (5m/15m) may be restricted to `intraday_start` (give it a generous buffer
    before the backtest window — detector warm-up only, contexts stay week-bounded)."""
    frames, dets = {}, {}
    for tf in CONTEXT_TFS:
        src = df_1m
        if intraday_start is not None and tf in ("5min", "15min"):
            src = df_1m[df_1m.index >= intraday_start]
        frames[tf] = resample_ohlcv(src, tf)
    for tf in CONTEXT_TFS:
        dets[tf] = run_all_detectors(frames[tf])
    return frames, dets


def run_engine(df_1m: pd.DataFrame, tf_store: dict, det_store: dict,
               decide_fn: Callable, start: pd.Timestamp, end: pd.Timestamp,
               label: str, equity0: float = ACCOUNT_EQUITY,
               max_decisions: Optional[int] = None, verbose: bool = False,
               peak0: Optional[float] = None) -> dict:
    """Event-driven paper engine over [start, end). `decide_fn(ctx_provider, price, t)`
    must return the dict produced by `llm_decide` (or a compatible strategy)."""
    sl_1m = df_1m[(df_1m.index >= start) & (df_1m.index < end)]
    times = sl_1m.index
    o, h, l, c = (sl_1m[k].to_numpy() for k in ("open", "high", "low", "close"))
    bar_close = (times + pd.Timedelta("1min")).asi8          # ns ints for set lookups

    dec = tf_store[DECISION_TF]
    ct = dec["close_time"]
    in_window = (ct > start) & (ct <= end)
    sess = pd.Series(in_ny_session(pd.DatetimeIndex(ct)), index=ct.index)
    all_closes = set(pd.DatetimeIndex(ct[in_window]).asi8)                # equity marks
    decision_set = set(pd.DatetimeIndex(ct[in_window & sess]).asi8)       # entry slots

    equity = float(equity0)
    peak = float(peak0) if peak0 is not None else equity                  # carries across folds
    position = pending = None
    trades, curve, halts = [], [], []
    counters = {"decision_slots": 0, "llm_consults": 0, "no_trade": 0, "gated": 0,
                "validator_rejected": 0, "llm_errors": 0, "retries": 0, "cache_hits": 0,
                "orders": 0, "fills": 0, "cancelled_gap": 0, "breaker_day_blocks": 0,
                "breaker_pause_blocks": 0, "breaker_halt_blocks": 0}
    cur_day = None
    day_start_equity = equity
    day_blocked = paused = halted = False
    consec_losses = 0

    def close_trade(i: int, px: float, reason: str) -> None:
        nonlocal position, equity, consec_losses, peak, day_blocked, paused, halted
        sign = 1.0 if position["direction"] == "long" else -1.0
        pnl = (px - position["entry"]) * sign * position["units"] \
              - SPREAD_PIPS * PIP * position["units"]            # round-trip spread
        equity += pnl
        trades.append({**position, "exit_time": times[i], "exit_px": px, "reason": reason,
                       "pnl_usd": pnl, "r_multiple": pnl / position["risk_usd"],
                       "equity_after": equity})
        consec_losses = consec_losses + 1 if pnl < 0 else 0
        if verbose:
            print(f"  [{times[i]}] EXIT {position['direction']} @ {px:.5f} ({reason}) "
                  f"pnl ${pnl:,.0f} → equity ${equity:,.0f}")
        position = None
        peak = max(peak, equity)
        if equity - day_start_equity <= -DAILY_LOSS_LIMIT * day_start_equity and not day_blocked:
            day_blocked = True
            halts.append({"time": times[i], "kind": "daily_loss_limit",
                          "detail": f"day pnl {equity - day_start_equity:,.0f}"})
        if consec_losses >= MAX_CONSEC_LOSSES and not paused:
            paused = True
            halts.append({"time": times[i], "kind": "consec_loss_pause",
                          "detail": f"{consec_losses} straight losses"})
        if (peak - equity) / peak >= MAX_DRAWDOWN_HALT and not halted:
            halted = True
            halts.append({"time": times[i], "kind": "max_drawdown_halt",
                          "detail": f"drawdown {(peak - equity) / peak:.1%}"})

    for i in range(len(times)):
        # 1 · fill pending order at this bar's open (the bar after the decision)
        if pending is not None and not halted:
            p = pending; pending = None
            fill = o[i]
            bad_gap = (p["direction"] == "long" and (fill <= p["stop_loss"] or fill >= p["take_profit"])) or \
                      (p["direction"] == "short" and (fill >= p["stop_loss"] or fill <= p["take_profit"]))
            if bad_gap:
                counters["cancelled_gap"] += 1
            else:
                risk_usd = equity * RISK_PCT
                units = risk_usd / abs(fill - p["stop_loss"])
                position = {"strategy": label, "direction": p["direction"],
                            "entry_time": times[i], "entry": fill,
                            "stop_loss": p["stop_loss"], "take_profit": p["take_profit"],
                            "units": units, "risk_usd": risk_usd,
                            "confidence": p["confidence"], "reasoning": p["reasoning"],
                            "decided_at": p["decided_at"]}
                counters["fills"] += 1
                if verbose:
                    print(f"  [{times[i]}] FILL {p['direction']} @ {fill:.5f} "
                          f"SL {p['stop_loss']:.5f} TP {p['take_profit']:.5f}")
        elif pending is not None:
            pending = None                                   # halted while order in flight

        # 2 · manage the open position on this 1m bar (SL first — pessimistic)
        if position is not None:
            if position["direction"] == "long":
                if l[i] <= position["stop_loss"]:
                    close_trade(i, position["stop_loss"], "SL")
                elif h[i] >= position["take_profit"]:
                    close_trade(i, position["take_profit"], "TP")
            else:
                if h[i] >= position["stop_loss"]:
                    close_trade(i, position["stop_loss"], "SL")
                elif l[i] <= position["take_profit"]:
                    close_trade(i, position["take_profit"], "TP")

        bt = bar_close[i]
        # 3 · decision slot at a session 15m close, flat & unblocked, breakers willing
        if bt in decision_set:
            t = times[i] + pd.Timedelta("1min")
            day = t.tz_localize("UTC").tz_convert(TZ_NY).date()
            if day != cur_day:                               # NY-day rollover
                cur_day, day_start_equity = day, equity
                day_blocked = paused = False
            counters["decision_slots"] += 1
            if max_decisions is not None and counters["llm_consults"] >= max_decisions:
                pass
            elif halted:
                counters["breaker_halt_blocks"] += 1
            elif day_blocked:
                counters["breaker_day_blocks"] += 1
            elif paused:
                counters["breaker_pause_blocks"] += 1
            elif position is None and pending is None:
                price = float(c[i])
                ctx_provider = (lambda tt=t: build_context(tf_store, det_store, tt))
                res = decide_fn(ctx_provider, price, t)
                counters["llm_consults"] += 1
                counters["cache_hits"] += int(res.get("cache_hit", False))
                counters["retries"] += max(0, res.get("attempts", 1) - 1)
                plan, status = res["plan"], res["status"]
                if status == "validator_rejected":
                    counters["validator_rejected"] += 1
                elif status == "llm_error":
                    counters["llm_errors"] += 1
                elif plan.direction == "none":
                    counters["no_trade"] += 1
                elif plan.confidence < CONF_THRESHOLD:
                    counters["gated"] += 1                   # confidence gate — no entry
                else:
                    pending = {"direction": plan.direction, "stop_loss": plan.stop_loss,
                               "take_profit": plan.take_profit, "confidence": plan.confidence,
                               "reasoning": plan.reasoning, "decided_at": t}
                    counters["orders"] += 1
                    if verbose:
                        print(f"[{t}] ORDER {plan.direction} conf {plan.confidence} — "
                              f"{plan.reasoning[:90]}")

        # 4 · mark-to-market equity at every 15m close in the window
        if bt in all_closes:
            mtm = equity
            if position is not None:
                sign = 1.0 if position["direction"] == "long" else -1.0
                mtm += (c[i] - position["entry"]) * sign * position["units"]
            curve.append((times[i] + pd.Timedelta("1min"), mtm))

    if position is not None:                                 # end of run: mark flat
        close_trade(len(times) - 1, float(c[-1]), "end_of_run")

    trades_df = pd.DataFrame(trades)
    curve_df = pd.DataFrame(curve, columns=["time", "equity"]).set_index("time")
    return {"label": label, "trades": trades_df, "curve": curve_df,
            "counters": counters, "halts": halts, "final_equity": equity,
            "final_peak": peak, "halted": halted}

print("simulator ready")
