# %% [markdown]
# ## 8 · Leakage safety — how look-ahead is prevented
#
# 1. **Decision-time convention.** A decision happens at a 15m bar close `t`. A piece
#    of information is admissible iff it was *knowable at or before* `t`.
# 2. **Closed candles only.** Every resampled bar carries `close_time = start + TF`;
#    the context builder admits bars with `close_time <= t`, so a partial current bar
#    can never leak in.
# 3. **Detector confirmation times.** Every detector record carries `confirmed_time` —
#    when the event became knowable (a k-pivot swing only k bars later; an FVG at its
#    third candle's close). Records are admitted iff `confirmed_time <= t`. The
#    prefix-consistency property test above machine-checks this for all 8 detectors.
# 4. **FVG open/filled status.** `fill_time` is precomputed over the full series for
#    speed, but is only ever **compared against `t`** (`filled iff fill_time <= t`),
#    which reveals exactly what an online observer would know at `t` — nothing more.
# 5. **Runtime guard.** `build_context()` collects every timestamp it serializes and
#    asserts `max(timestamps) < t` *(bar starts and event times all strictly precede
#    the decision close)* plus `confirmed_time <= t` for every record. A violation
#    raises `LeakageError` and kills the run.
# 6. **Next-bar fills.** Orders decided at `t` fill at the open of the *next* 1m bar —
#    the simulator never fills on a close the LLM has just seen.
# 7. **Context recency bound.** Per the requirement, context may reach back at most to
#    the start of the **previous ISO week** (Monday 00:00 UTC); intra-week bars plus
#    the week-bounded HTF view. (Structure/trend state is accumulated from earlier
#    history — past information only, which is always admissible.)

# %%
class LeakageError(AssertionError):
    """Raised when anything in a context would not have been knowable at decision time."""


def context_window_start(t: pd.Timestamp) -> pd.Timestamp:
    """Monday 00:00 UTC of the *previous* ISO week relative to t."""
    monday_this = (t - pd.Timedelta(days=int(t.dayofweek))).normalize()
    return monday_this - pd.Timedelta(weeks=1)


def visible(records: pd.DataFrame, t: pd.Timestamp,
            since: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Records knowable at t (confirmed_time <= t), optionally within the recency window."""
    if records.empty:
        return records
    m = records["confirmed_time"] <= t
    if since is not None:
        m &= records["time"] >= since
    return records[m]


# bars of each TF shown to the LLM (most recent, capped, within the week window).
# Sized for local prefill speed: ~78 tok/s on an M4-16GB makes every context byte a
# latency cost, so the bar tables stay lean and detector summaries carry the load.
BARS_IN_CONTEXT = {"5min": 12, "15min": 16, "1h": 12, "4h": 8, "1d": 6}
MAX_RECORDS_PER_TYPE = 3


def _fmt_px(x: float) -> str:
    return f"{x:.5f}"


def _fmt_bars(df: pd.DataFrame, tf: str) -> str:
    fmt = "%m-%d" if tf == "1d" else "%m-%d %H:%M"
    lines = [f"  {ts.strftime(fmt)}  O {r.open:.5f}  H {r.high:.5f}  L {r.low:.5f}  C {r.close:.5f}"
             for ts, r in zip(df.index, df.itertuples())]
    return "\n".join(lines)


def build_context(tf_store: dict, det_store: dict, t: pd.Timestamp) -> str:
    """Assemble the point-in-time multi-timeframe context block for decision time t.

    Every bar shown has close_time <= t; every detector record has confirmed_time <= t;
    nothing predates the start of the previous ISO week. A runtime leakage check runs
    before the string is returned.
    """
    t = pd.Timestamp(t)
    since = context_window_start(t)
    audit_times: list[pd.Timestamp] = []      # every timestamp that gets serialized
    sections = []

    # current price = close of the most recent closed decision-TF bar
    dec = tf_store[DECISION_TF]
    closed = dec[dec["close_time"] <= t]
    if closed.empty:
        raise LeakageError(f"no closed {DECISION_TF} bar at {t}")
    price = float(closed["close"].iloc[-1])

    ny_t = t.tz_localize("UTC").tz_convert(TZ_NY)
    header = (
        f"DECISION TIME : {t} UTC  ({ny_t.strftime('%Y-%m-%d %H:%M')} New York)\n"
        f"CURRENT PRICE : {_fmt_px(price)} (close of the last completed {DECISION_TF} bar)\n"
        f"SESSION       : New York ({NY_SESSION_START}-{NY_SESSION_END} ET) — entries allowed now\n"
        f"SPREAD        : {SPREAD_PIPS:.1f} pip fixed | history shown back to {since.date()} (previous week start)"
    )

    for tf in CONTEXT_TFS:
        bars_df = tf_store[tf]
        vis_bars = bars_df[(bars_df["close_time"] <= t) & (bars_df.index >= since)]
        vis_bars = vis_bars.tail(BARS_IN_CONTEXT[tf])
        audit_times.extend(vis_bars.index)
        d = det_store[tf]
        lines = [f"--- {tf.upper()} ---", "bars (oldest first):", _fmt_bars(vis_bars, tf)]

        ev_all = visible(d["structure"], t)            # trend = accumulated state (history)
        trend = {1: "UP", -1: "DOWN", 0: "undetermined"}[int(ev_all["trend_after"].iloc[-1])] if len(ev_all) else "undetermined"
        lines.append(f"trend after last structure event: {trend}")
        for r in ev_all[ev_all["time"] >= since].tail(3).itertuples():   # listed events stay week-bounded
            audit_times.append(r.time)
            lines.append(f"  structure: {r.kind} of {_fmt_px(r.level)} at {r.time}")

        sw = visible(d["swings"], t, since).tail(MAX_RECORDS_PER_TYPE)
        for r in sw.itertuples():
            audit_times.append(r.time)
            tag = f" ({r.label})" if isinstance(r.label, str) else ""
            lines.append(f"  swing {r.kind}{tag}: {_fmt_px(r.level)} at {r.time}")

        fv = visible(d["fvg"], t, since)
        if len(fv):
            open_mask = fv["fill_time"].isna() | (fv["fill_time"] > t)   # leakage-safe compare
            fv_open = fv[open_mask].copy()
            fv_open["dist"] = (fv_open[["top", "bottom"]].mean(axis=1) - price).abs()
            for r in fv_open.nsmallest(MAX_RECORDS_PER_TYPE, "dist").sort_values("time").itertuples():
                audit_times.append(r.time)
                touched = "touched" if (pd.notna(r.touch_time) and r.touch_time <= t) else "untouched"
                lines.append(f"  open {r.direction} FVG: {_fmt_px(r.bottom)}-{_fmt_px(r.top)} "
                             f"({touched}) formed {r.time}")

        for r in visible(d["order_blocks"], t, since).tail(MAX_RECORDS_PER_TYPE).itertuples():
            audit_times.append(r.time)
            lines.append(f"  {r.direction} order block: {_fmt_px(r.bottom)}-{_fmt_px(r.top)} at {r.time}")

        for r in visible(d["sweeps"], t, since).tail(MAX_RECORDS_PER_TYPE).itertuples():
            audit_times.append(r.time)
            lines.append(f"  liquidity sweep ({r.side}): level {_fmt_px(r.swept_level)} "
                         f"wick {_fmt_px(r.wick_extreme)} at {r.time}")

        for r in visible(d["pools"], t, since).tail(MAX_RECORDS_PER_TYPE).itertuples():
            audit_times.append(r.time)
            lines.append(f"  liquidity pool {r.pool} ({r.side} resting stops): {_fmt_px(r.level)}")

        pdd = premium_discount(visible(d["swings"], t, since), price)
        if pdd:
            lines.append(f"  dealing range {_fmt_px(pdd['range_low'])}-{_fmt_px(pdd['range_high'])}, "
                         f"equilibrium {_fmt_px(pdd['equilibrium'])} → price in {pdd['zone'].upper()} "
                         f"({pdd['position']:.0%} of range)")

        for r in visible(d["displacement"], t, since).tail(2).itertuples():
            audit_times.append(r.time)
            lines.append(f"  displacement ({r.direction}) at {r.time}")

        sections.append("\n".join(lines))

    # ---- runtime leakage guard: everything serialized must predate the decision close
    if audit_times:
        worst = max(audit_times)
        if not worst < t:
            raise LeakageError(f"context for {t} contains timestamp {worst} >= decision time")
    return header + "\n\n" + "\n\n".join(sections)

print("context builder ready")

# %% [markdown]
# ## 9 · LLM decision layer
#
# The system prompt is an ICT primer distilled from the same researched sources as the
# detectors, plus a strict output contract. Generation parameters are pinned
# (temperature 0, fixed seed) and JSON output is requested natively from the provider,
# so a given context string reproducibly maps to the same `TradePlan`.
#
# **Provider note:** `LLM_PROVIDER` in the config selects local Ollama (the original
# spec: local LLM, no internet, no paid APIs) or the Gemini API — a **documented
# temporary exception** taken because this machine sustains only ~35-45s per local
# 8B call, while reasoning quality was the priority. Both providers share the same
# prompt, validator, cache and audit-log path, so results stay comparable.

# %%
SYSTEM_PROMPT = f"""You are a disciplined intraday EUR/USD trader using ICT (Inner Circle Trader) /
Smart Money Concepts. You receive a point-in-time multi-timeframe market snapshot and must decide:
long, short, or none.

CONCEPT PRIMER (how to read the context):
- Fair Value Gap (FVG): a 3-candle imbalance left by a displacement candle. Price frequently
  retraces into an open FVG before continuing. An untouched FVG in your trade direction is
  a high-quality entry zone.
- Order Block (OB): the last opposing candle before a displacement that created an FVG —
  where institutional orders entered. Expect reactions when price returns to the zone.
- Liquidity Sweep: a wick through a prior swing high/low that closes back inside — a stop
  raid. A sell-side sweep (below lows) is bullish; a buy-side sweep (above highs) is bearish.
- BOS (Break of Structure): close beyond a swing WITH the trend — continuation.
  CHoCH (Change of Character): close beyond a counter-trend swing — first reversal warning.
- Liquidity pools (EQH/EQL): equal highs/lows where stops cluster; price gravitates there.
- Premium/Discount: equilibrium is 50% of the dealing range. Prefer LONGS in DISCOUNT and
  SHORTS in PREMIUM. Avoid chasing entries deep against this rule.
- The classic A+ sequence: liquidity sweep -> CHoCH/MSS with displacement -> entry on the
  retrace into the FVG/OB, stop beyond the sweep wick, in the right premium/discount zone,
  aligned with the higher-timeframe trend.

DECISION RULES (binding):
1. Direction "none" is the correct call most of the time. Only trade clear multi-timeframe
   confluence: HTF (1h/4h/1d) bias + a recent sweep or structure shift on 5m/15m + sensible
   premium/discount location.
2. Entry must be a market order at (or within a few pips of) CURRENT PRICE — fills happen
   on the next bar. Do not place limit orders far from price.
3. stop_loss goes beyond a real structural level (sweep wick, OB edge, FVG far edge),
   on the correct side of entry.
4. take_profit MUST equal entry +/- {RR_TARGET:.0f} x (the entry-to-stop distance): exactly
   1:{RR_TARGET:.0f} risk:reward. Plans violating this are rejected.
5. confidence is 0-100. Use >= {CONF_THRESHOLD} ONLY for A+ setups (rule 1 fully satisfied).
   Trades below {CONF_THRESHOLD} are skipped automatically — be honest, not eager.
6. Costs: spread is {SPREAD_PIPS:.1f} pip; stops tighter than ~5 pips are noise — avoid them.

OUTPUT CONTRACT — respond with ONLY this JSON object, no code fences, no extra text:
{{"direction": "long" | "short" | "none",
  "entry": <float>, "stop_loss": <float>, "take_profit": <float>,
  "confidence": <int 0-100>, "reasoning": "<one or two short sentences>"}}
For "none", entry/stop_loss/take_profit may be 0."""


@dataclass
class TradePlan:
    direction: str                    # "long" | "short" | "none"
    entry: float = 0.0
    stop_loss: float = 0.0
    take_profit: float = 0.0
    confidence: int = 0
    reasoning: str = ""

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def ollama_generate(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """One pinned-parameter, non-streaming Ollama call. Text in, text out.
    `keep_alive` holds the model in memory between calls (on a 16GB machine Ollama
    otherwise unloads it, costing a ~12s reload and the occasional dropped
    connection). Transient connection errors are retried with backoff — distinct
    from the validator's semantic retry."""
    est_tokens = (len(system) + len(prompt)) // 3 + OLLAMA_PARAMS["num_predict"]
    num_ctx = 4096 if est_tokens < 3800 else 8192      # smaller KV cache when it fits —
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "system": system,    # this is a
               "stream": False, "format": "json", "keep_alive": "60m",       # 16GB machine
               "options": {**OLLAMA_PARAMS, "num_ctx": num_ctx}}
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(3):
        try:
            r = requests.post(OLLAMA_URL, json=payload, timeout=OLLAMA_TIMEOUT)
            r.raise_for_status()
            return r.json()["response"]
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            _time.sleep(2 * (attempt + 1))
    raise last_err


def gemini_generate(prompt: str, system: str = SYSTEM_PROMPT) -> str:
    """Gemini API call with the same contract as `ollama_generate` (text in, text out).
    TEMPORARY EXCEPTION to the original local-only/no-paid-API spec — adopted for
    reasoning quality + speed on memory-constrained hardware; see the config cell.
    Pinned params + JSON response mime type; 429/5xx retried with backoff (free-tier
    rate limits surface as 429s)."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set — export it or set LLM_PROVIDER='ollama'")
    body = {"system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {**GEMINI_PARAMS, "responseMimeType": "application/json"}}
    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(4):
        try:
            r = requests.post(GEMINI_URL, json=body, timeout=GEMINI_TIMEOUT,
                              headers={"x-goog-api-key": GEMINI_API_KEY})
            if r.status_code in (429, 500, 502, 503):
                last_err = RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                _time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            cands = r.json().get("candidates", [])
            if not cands or "content" not in cands[0]:
                raise RuntimeError(f"no candidates in response: {r.text[:200]}")
            return "".join(p.get("text", "") for p in cands[0]["content"].get("parts", []))
        except (requests.ConnectionError, requests.Timeout) as e:
            last_err = e
            _time.sleep(5 * (attempt + 1))
    raise last_err


DEFAULT_GENERATE: Callable[[str], str] = (
    gemini_generate if LLM_PROVIDER == "gemini" else ollama_generate)
ACTIVE_LLM_PARAMS = GEMINI_PARAMS if LLM_PROVIDER == "gemini" else OLLAMA_PARAMS


def parse_trade_plan(text: str) -> tuple[Optional[TradePlan], str]:
    """Defensive parse: strip code fences, isolate the outermost JSON object, coerce
    types, clamp confidence to [0, 100]. Returns (plan, "") or (None, reason)."""
    s = re.sub(r"```(?:json)?", "", text).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None, "no JSON object found"
    s = s[start:end + 1]
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        try:
            obj = json.loads(re.sub(r",\s*([}\]])", r"\1", s))   # drop trailing commas
        except json.JSONDecodeError as e:
            return None, f"malformed JSON: {e}"
    if not isinstance(obj, dict):
        return None, "JSON is not an object"
    direction = str(obj.get("direction", "")).lower().strip()
    if direction not in ("long", "short", "none"):
        return None, f"bad direction {obj.get('direction')!r}"
    try:
        plan = TradePlan(
            direction=direction,
            entry=float(obj.get("entry") or 0.0),
            stop_loss=float(obj.get("stop_loss") or 0.0),
            take_profit=float(obj.get("take_profit") or 0.0),
            confidence=int(max(0, min(100, float(obj.get("confidence") or 0)))),
            reasoning=str(obj.get("reasoning", ""))[:500],
        )
    except (TypeError, ValueError) as e:
        return None, f"bad field types: {e}"
    return plan, ""

print("LLM layer ready —", LLM_MODEL_ID)

# %% [markdown]
# ## 10 · Validator
#
# A plan is **rejected** when the implied reward:risk violates the configured 1:2
# (outside `RR_TARGET ± RR_TOLERANCE`), when the levels are on the wrong sides of the
# entry, or when the entry strays so far from current price that a next-bar market fill
# would not resemble it. On rejection the LLM is re-prompted **once** with the reason;
# a second failure skips the trade (`direction: none`) and logs why.

# %%
MAX_ENTRY_DRIFT_PIPS = 10.0   # entry must be a market-order near current price

def validate_plan(plan: TradePlan, price: float) -> tuple[bool, str]:
    if plan.direction == "none":
        return True, "no-trade"
    e, sl, tp = plan.entry, plan.stop_loss, plan.take_profit
    if min(e, sl, tp) <= 0:
        return False, "non-positive level(s)"
    if plan.direction == "long" and not (sl < e < tp):
        return False, f"long needs SL<entry<TP, got SL={sl} E={e} TP={tp}"
    if plan.direction == "short" and not (tp < e < sl):
        return False, f"short needs TP<entry<SL, got TP={tp} E={e} SL={sl}"
    risk, reward = abs(e - sl), abs(tp - e)
    if risk < 1e-9:
        return False, "zero risk distance"
    rr = reward / risk
    if abs(rr - RR_TARGET) > RR_TOLERANCE:
        return False, f"R:R {rr:.2f} violates target {RR_TARGET:.1f}±{RR_TOLERANCE}"
    if abs(e - price) > MAX_ENTRY_DRIFT_PIPS * PIP:
        return False, (f"entry {e:.5f} is {abs(e - price) / PIP:.1f} pips from current "
                       f"price {price:.5f} (max {MAX_ENTRY_DRIFT_PIPS:.0f})")
    return True, "ok"

# quick self-checks
_p = TradePlan("long", 1.1000, 1.0990, 1.1020, 80)
assert validate_plan(_p, 1.1001)[0]
assert not validate_plan(TradePlan("long", 1.1000, 1.0990, 1.1010, 80), 1.1001)[0]   # 1:1
assert not validate_plan(TradePlan("short", 1.1000, 1.0990, 1.1020, 80), 1.1001)[0]  # sides wrong
assert not validate_plan(_p, 1.1050)[0]                                              # entry drift
assert validate_plan(TradePlan("none"), 1.1)[0]
print("validator ready")

# %% [markdown]
# ## 11 · Cache & audit log
#
# - **Cache:** responses keyed by SHA-256 of (model + pinned params + system prompt +
#   exact context string). Identical context → identical TradePlan with zero recompute.
#   The cached record stores the *final* post-retry outcome.
# - **Audit log:** every attempt (including retries and cache hits) appends a JSONL
#   line with the raw prompt, raw response, parsed plan, validation verdict and latency.

# %%
RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

class LLMCache:
    def __init__(self, root: Path):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        self.hits = self.misses = 0

    @staticmethod
    def key(context: str, system: str = SYSTEM_PROMPT) -> str:
        blob = "|".join([LLM_MODEL_ID, json.dumps(ACTIVE_LLM_PARAMS, sort_keys=True),
                         system, context])
        return hashlib.sha256(blob.encode()).hexdigest()

    def get(self, key: str) -> Optional[dict]:
        p = self.root / f"{key}.json"
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text())
        self.misses += 1
        return None

    def put(self, key: str, record: dict) -> None:
        (self.root / f"{key}.json").write_text(json.dumps(record, default=str))


class JsonlLogger:
    def __init__(self, path: Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.n = 0

    def log(self, **fields) -> None:
        fields["logged_at"] = datetime.now().isoformat(timespec="seconds")
        with self.path.open("a") as f:
            f.write(json.dumps(fields, default=str) + "\n")
        self.n += 1


def llm_decide(context: str, price: float, cache: LLMCache, logger: JsonlLogger,
               generate_fn: Optional[Callable[[str], str]] = None,
               decision_time=None) -> dict:
    """Cached, validated, retry-once LLM decision. Returns
    {plan, status, cache_hit, attempts, reject_reason}."""
    generate_fn = generate_fn or DEFAULT_GENERATE
    key = cache.key(context)
    cached = cache.get(key)
    if cached is not None:
        logger.log(event="cache_hit", decision_time=decision_time, key=key,
                   plan=cached["plan"], status=cached["status"])
        return {"plan": TradePlan(**cached["plan"]), "status": cached["status"],
                "cache_hit": True, "attempts": 0, "reject_reason": cached.get("reject_reason", "")}

    prompt, status, reject_reason = context, "", ""
    plan: Optional[TradePlan] = None
    for attempt in (1, 2):
        t0 = _time.time()
        try:
            raw = generate_fn(prompt)
        except Exception as e:                      # Ollama down / timeout → no trade
            logger.log(event="llm_error", decision_time=decision_time, key=key,
                       attempt=attempt, error=repr(e))
            plan, status, reject_reason = TradePlan("none"), "llm_error", repr(e)
            break
        latency = _time.time() - t0
        cand, perr = parse_trade_plan(raw)
        if cand is None:
            ok, reason = False, f"parse failure: {perr}"
        else:
            ok, reason = validate_plan(cand, price)
        logger.log(event="llm_call", decision_time=decision_time, key=key, attempt=attempt,
                   raw_prompt=prompt, raw_response=raw, latency_s=round(latency, 2),
                   parsed=cand.to_dict() if cand else None, valid=ok, reason=reason)
        if ok:
            plan, status = cand, "ok"
            break
        if attempt == 1:                            # retry once, with the reason inline
            prompt = (context + "\n\nYOUR PREVIOUS RESPONSE WAS REJECTED: " + reason +
                      "\nRespond again. Output ONLY the JSON object, exactly per the contract.")
            status, reject_reason = "retried", reason
        else:                                       # second failure → skip the trade
            plan, status, reject_reason = TradePlan("none"), "validator_rejected", reason
    if status != "llm_error":          # never cache transport failures — a re-run
        record = {"plan": plan.to_dict(), "status": status,        # should retry them
                  "reject_reason": reject_reason, "decision_time": str(decision_time)}
        cache.put(key, record)
    return {"plan": plan, "status": status, "cache_hit": False,
            "attempts": attempt, "reject_reason": reject_reason}

print(f"cache + logger ready (run id {RUN_ID})")
