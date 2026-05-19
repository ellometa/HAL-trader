# Phase 9 — Extension polish + conversation memory

> **Agent brief.** You are an agent executing this phase end-to-end on
> the HAL repo. Phase 8 just shipped (commit `6aaa3c5`). Read
> `plans/00_overview.md` first if you haven't seen the project. The five
> items below are ordered; do them in order, commit after each, push at
> the end. Match HAL's existing code style — terse, comment-the-WHY-only,
> no comments restating identifiers. Vanilla JS, no bundler, no TS, no
> framework. Tests for Python via `uv run pytest tests/ -q`.

## Goal

Phase 8 sharpened answers (BOS/CHoCH, liquidity, cache, debug expander).
Phase 9 sharpens the *surface* around them: multi-turn context,
abortable streams, robust chart parsing, a Pine visual rail, and a
defensive style boundary.

## Snapshot (what exists right now)

- `backend/main.py` — FastAPI, SSE `/analyze`, blocking `/analyze_blocking`,
  module-level `_State` with `notes` + `client`. No session memory.
- `backend/prompts.py` — `build_system_prompt(notes)` and
  `build_user_message(symbol, tf, price, features, query)`. Both return
  plain strings. IST conversion at the prompt boundary.
- `backend/ict/*` — fvg, order_blocks, structure, liquidity. All wired
  into `detect_all`.
- `backend/ohlc.py` — 60s OHLC cache (P7 from phase 8).
- `extension/content.js` — single-file content script (~430 lines).
  Floating FAB + panel. SSE consumer with `meta` / `token` / `error` /
  `done` events. `metaPayload` is stashed and drives the
  `appendDebugExpander` accordion.
- `extension/panel.css` — `.hal-*` class prefix, no Shadow DOM yet.
- `extension/manifest.json` — MV3, content_scripts inject content.js +
  panel.css into `*.tradingview.com`.
- Per-chart history in `chrome.storage.local` keyed by
  `hal_chat_<slug>`, capped 50. Per-chart TF in `hal_tf_<slug>`.

## Read these files before starting

- `extension/content.js` (whole file — 430 lines)
- `extension/panel.css`
- `extension/manifest.json`
- `backend/main.py`
- `backend/prompts.py`
- `plans/00_overview.md`
- `plans/ideation/03_frontend_viz.md` and
  `plans/ideation/05_undo_candidates.md` — design decisions referenced
  below.

---

## Scope (execute in order)

### P2. Conversation memory (per-tab, in-memory server-side)

Every `/analyze` is independent today. Multi-turn ("what about the 1h?")
falls flat because Gemini sees zero prior context.

#### Backend changes — `backend/main.py`

Extend `AnalyzeRequest`:

```python
class AnalyzeRequest(BaseModel):
    symbol: str
    timeframe: str
    query: str
    session_id: str | None = None
```

Add a module-level session store next to `_state`:

```python
# (session_id) -> list of {"role": "user"|"assistant", "text": str, "ts": float}
_sessions: dict[str, list[dict[str, Any]]] = {}
_SESSION_TURN_CAP = 20         # turns per session before truncation
_SESSION_IDLE_TIMEOUT = 3600   # seconds of inactivity before eviction
_HISTORY_TURNS_TO_SEND = 6     # last N turns prepended to Gemini contents
```

In `_prepare` (or a new helper), build `contents` for Gemini as a list:

```python
# google-genai accepts a list of Content / dict items as `contents`.
# Each item is {"role": "user"|"model", "parts": [{"text": str}]}.
# Currently we pass a single string user_msg; widen to a list when
# session_id is provided.
```

Append the *new* user turn to the session after a successful response.
The assistant turn (full streamed text) is appended at stream end —
accumulate `text` chunks as they're yielded, then store.

Eviction sweep: at the top of each request, drop sessions whose last
turn is older than `_SESSION_IDLE_TIMEOUT`. Cheap O(N_sessions) walk;
for a personal tool with ≤10 sessions, this is free.

Critical: the *features* portion of the user message stays per-call.
Don't replay stale features from prior turns — only the question/answer
text. Build it as:

```python
contents = []
for turn in session_history[-_HISTORY_TURNS_TO_SEND:]:
    role = "user" if turn["role"] == "user" else "model"
    contents.append({"role": role, "parts": [{"text": turn["text"]}]})
contents.append({"role": "user", "parts": [{"text": user_msg}]})
```

Both `/analyze` and `/analyze_blocking` need this. Keep `_prepare`
returning what it returns; build `contents` after the call.

#### Extension changes — `extension/content.js`

Add a session-id storage helper next to the existing TF/chat helpers:

```js
const SESSION_STORAGE_PREFIX = "hal_session_";
let sessionStorageKey = null;
let sessionId = null;
```

On panel open (or `deriveStorageKeys`), load-or-create the session id:

```js
async function loadOrCreateSession() {
  if (!chrome?.storage?.local) {
    sessionId = crypto.randomUUID();
    return;
  }
  return new Promise((resolve) => {
    chrome.storage.local.get(sessionStorageKey, (obj) => {
      sessionId = obj[sessionStorageKey] || crypto.randomUUID();
      chrome.storage.local.set({ [sessionStorageKey]: sessionId }, resolve);
    });
  });
}
```

Send `session_id` in every POST body. "Clear chat" rotates it:

```js
$clear.addEventListener("click", () => {
  history = [];
  clearHistory();
  sessionId = crypto.randomUUID();
  chrome.storage.local.set({ [sessionStorageKey]: sessionId });
  renderMessages(history);
});
```

#### Acceptance

- Open BTC chart, ask "what's the structure?". Then ask "what about the
  1h?" without specifying symbol. HAL answers about BTC 1h.
- "Clear chat" produces a fresh session — follow-up no longer carries
  forward.
- `pytest tests/ -q` still green.
- Add no new Python test for sessions (in-memory dict is too thin to
  warrant). Manual verification only.

---

### P3. Abort button + visible thinking state

Hook `AbortController` into the streaming fetch. While streaming, Send
becomes Stop (same button, swapped label + handler).

#### Changes in `extension/content.js`

```js
let inflightController = null;

async function send() {
  // ... existing setup ...
  inflightController = new AbortController();
  $send.textContent = "Stop";
  $send.classList.add("hal-stop");

  try {
    const resp = await fetch(BACKEND, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, timeframe: selectedTimeframe, query: text, session_id: sessionId }),
      signal: inflightController.signal,
    });
    // ... existing stream loop with signal.aborted handling ...
  } catch (err) {
    if (err.name === "AbortError") {
      streamErr = null; // not an error
    } else {
      streamErr = `Could not reach backend at ${BACKEND}. Is uvicorn running? (${err.message})`;
    }
  } finally {
    $send.textContent = "Send";
    $send.classList.remove("hal-stop");
    inflightController = null;
    // ... existing finally body ...
  }
}

$send.addEventListener("click", () => {
  if (inflightController) {
    inflightController.abort();
  } else {
    send();
  }
});
```

When the user aborts: stop loop, append `"(stopped)"` to the in-flight
bubble (not as an error), persist the partial as an assistant turn in
session memory (yes — agent partial is real context).

Thinking state: the existing `thinking…` bubble already covers
pre-token latency. Keep it. Optionally style it with a 3-dot animation
in CSS (`hal-msg-thinking::after { content: ""; animation: ...; }`),
but only if you can do it in < 15 lines of CSS.

#### Acceptance

- Click Stop mid-stream → tokens stop within a frame, bubble closes
  with "(stopped)", panel ready for next input.
- No `AbortError` in console.
- Aborted partial is included in the session memory (verifiable by
  asking a follow-up question that depends on the aborted text).

---

### P4. Robust symbol detection

`extension/content.js` has both `symbolFromUrl()` and `symbolFromTitle()`.
The title fallback hasn't fired in real usage. Per
`plans/ideation/05_undo_candidates.md` item #5: either delete or
harden it. Test before deciding.

#### Test matrix (do this manually first)

Visit each chart type, record the URL and what `?symbol=` returns:

| Chart type | URL pattern | `?symbol=` | Notes |
|------------|-------------|------------|-------|
| Spot crypto | `/chart/?symbol=BINANCE:BTCUSDT` | BTCUSDT | ✓ |
| Perp futures | `/chart/?symbol=BINANCE:BTCUSDT.P` | BTCUSDT.P | strip `.P`? |
| Stock | `/chart/?symbol=NASDAQ:AAPL` | AAPL | ✓ |
| Index | `/chart/?symbol=TVC:SPX` or `?symbol=SP:SPX` | SPX | ✓ |
| Forex pair | `/chart/?symbol=FX:EURUSD` | EURUSD | ✓ |
| Futures (month) | `/chart/?symbol=CME_MINI:ES1!` | ES1! | continuous contract |

Likely outcome: `?symbol=` covers all six. Delete `symbolFromTitle()`.
Surface a visible panel error if URL parsing fails.

#### Changes

If deleting fallback:
- Drop `symbolFromTitle()` function and its caller in `detectSymbol()`.
- Update `detectSymbol()` to return `null` when URL parse fails.
- The existing "Couldn't detect a symbol on this page" error already
  handles the null case in `send()`. Confirm it's user-friendly.

If keeping a narrowed fallback: document each case it handles in a
comment block above the function.

Add a comment block at the top of `content.js` recording the verified
chart types (becomes a regression check for future you):

```js
// Symbol parsing verified against TradingView chart types (2026-05-19):
//   spot crypto, perp futures (.P suffix), stock, index, forex, futures.
// All present `?symbol=EXCHANGE:TICKER` in the URL. Title fallback
// removed — never fired in practice.
```

#### Acceptance

- Spot-check all six chart types from the matrix.
- Failure case (e.g. TV homepage) produces the panel error, not a
  silent wrong-symbol POST.

---

### P9. Shadow DOM for the extension panel

Wrap the panel root in a Shadow DOM. CSS isolation in both directions
— TV's stylesheets can't bleed into HAL, HAL's can't bleed into TV.

#### Changes in `extension/content.js`

Replace the direct `document.body.appendChild(panel)` flow with:

```js
const host = document.createElement("div");
host.id = "hal-host";
document.body.appendChild(host);
const shadow = host.attachShadow({ mode: "open" });

// Inject stylesheet into the shadow root.
const style = document.createElement("style");
style.textContent = `__INLINE_CSS_HERE__`;  // see below
shadow.appendChild(style);

// FAB + panel go inside the shadow root.
shadow.appendChild(fab);
shadow.appendChild(panel);
```

#### CSS inlining

Two options:

**Option A — fetch panel.css at runtime via the extension's bundled URL.**

```js
const cssUrl = chrome.runtime.getURL("panel.css");
const cssText = await fetch(cssUrl).then((r) => r.text());
style.textContent = cssText;
```

Add `panel.css` to `web_accessible_resources` in `manifest.json`.
Remove `panel.css` from `content_scripts.css` (the global injection).

**Option B — read panel.css contents into a constant at build time.**

We have no build step. Skip.

Use Option A.

#### Mouse event stopPropagation

The existing `for (const ev of [...])` loop on `panel.addEventListener`
still works, but now targets a node inside the shadow root. TradingView
listens on the document — events bubble *through* shadow boundaries
unless stopped. Move the listener to the host element instead:

```js
for (const ev of ["mousedown", "mouseup", "mousemove", "click", "dblclick", "wheel"]) {
  host.addEventListener(ev, (e) => e.stopPropagation());
}
```

#### Query selectors

`$ctx`, `$tf`, `$messages`, `$input`, `$send`, `$clear`, `$close` all
become `shadow.querySelector(...)` instead of `panel.querySelector(...)`.
Update those binding lines.

#### Acceptance

- Toggle TV's light/dark theme — HAL panel styling unchanged.
- Inject `* { color: red }` via DevTools on the page → HAL text stays
  the right color (because shadow CSS isolates it).
- All buttons still work (TF select, clear, close, send/stop).
- `chrome.storage.local` access is unaffected (it always was).

---

### P4b. Pine Script FVG / OB highlighter

Two standalone Pine v6 indicators paste-once-and-leave-on-chart. They
mirror `backend/ict/fvg.py` and `backend/ict/order_blocks.py` so the
user sees the *same* zones HAL is reasoning about, drawn natively in
TradingView.

**Critical:** detection rule must be identical to the Python. Numbers
have to line up to the cent / candle. If you can't port a Python edge
case to Pine, document the gap in a comment in the Pine file rather
than silently differing.

#### Files to add

- `pine/hal_fvg.pine`
- `pine/hal_ob.pine`

#### Inputs (Pine v6)

`hal_fvg.pine`:

```pine
//@version=6
indicator("HAL FVG", overlay = true, max_boxes_count = 500)

show_bull = input.bool(true, "Bullish FVG")
show_bear = input.bool(true, "Bearish FVG")
show_mit  = input.bool(true, "Show mitigated")
col_bull  = input.color(color.new(color.green, 80), "Bullish color")
col_bear  = input.color(color.new(color.red,   80), "Bearish color")
col_mit   = input.color(color.new(color.gray,  85), "Mitigated color")
```

`hal_ob.pine` adds:

```pine
swing_n = input.int(2, "Swing N", minval=1, maxval=10)
```

#### Detection — keep Python and Pine in lockstep

FVG (mirror `backend/ict/fvg.py`):

```pine
// Bullish: low[0] > high[2]
// Bearish: high[0] < low[2]
// (Pine's [n] is "n bars back" — careful with index direction.)
bull_fvg = low > high[2]
bear_fvg = high < low[2]
```

When triggered: draw a box from bar `[2]` to bar `[0]` between the
relevant prices. Track mitigation as later bars overlap the box;
recolor on mit if `show_mit`.

OB (mirror `backend/ict/order_blocks.py`):

- Detect swing highs/lows with strict-greater on both sides over
  `swing_n` bars.
- Track unbroken swings.
- On close > swing high (or close < swing low), walk back to find the
  last opposing candle. Mark that candle's full wick range as the OB
  box. Mitigation: any later candle overlapping the box.

#### Acceptance

- On BTC 4h: paste `hal_fvg.pine` into TV's Pine editor, add to chart.
  Count Pine's bullish + bearish FVG boxes. Hit `/analyze_blocking` for
  the same symbol/timeframe, count `fvgs`. Numbers match.
- Same for OB.
- Mitigated zones render visually distinct (faded or hatched).

---

## Out of scope for phase 9

- Persistent journaling, SQLite, server-side history — separate plan
  after v0 (see `plans/ideation/02_data_and_state.md`).
- Detector toggles (P13), eval harness (P14), prompt tuning (P10).
- TypeScript migration, framework adoption — park per
  `plans/ideation/03_frontend_viz.md`.
- launchd auto-start, `hal serve` CLI — see
  `plans/ideation/04_process_topology.md` (queue as P17 if desired).
- Anything in `plans/ideation/05_undo_candidates.md` *other than* the
  symbol-detection fallback covered here.

## Commit cadence

One commit per item, in order. Suggested messages (HAL style — lowercase
prefix, terse, why-focused):

- `phase 9 P2: per-session conversation memory`
- `phase 9 P3: abort button + stop control`
- `phase 9 P4: pin symbol detection to URL ?symbol=`
- `phase 9 P9: shadow DOM panel root`
- `phase 9 P4b: pine FVG + OB highlighters`

End-of-phase commit message for any final cleanup:
`phase 9: extension polish + conversation memory`. Push at the end.

## Definition of done

- All five items shipped, committed, pushed.
- `uv run pytest tests/ -q` green.
- Manual end-to-end (record in chat, don't write a doc):
  - Open BTC 4h chart.
  - Ask "what's the structure?". Then "what about the 1h?" — HAL
    answers about BTC 1h without you naming the symbol.
  - Start a long answer, click Stop mid-stream → graceful stop.
  - Switch to AAPL — fresh chart slug → fresh session.
  - Verify Pine FVG/OB counts on BTC 4h match HAL's reported counts in
    the debug expander.
  - Inject `* { color: red }` on TradingView's page → HAL panel
    unaffected.
- Update README "Status" section to note phase 9 done and what's next.

## Working-style reminders

- Prefer Edit over Write for existing files. Don't recreate
  `content.js` wholesale — there are mouse-stopPropagation hacks and
  selectors that need preserving.
- Tests stay in `tests/`. Run `uv run pytest tests/ -q` before committing.
- Don't introduce CORS, env var, or dependency changes that aren't
  spelled out above. If something forces one, stop and ask.
- Keep comments to one short line per non-obvious WHY. No multi-line
  block comments. No restating identifiers.
- Don't run `git push --force` and don't amend already-pushed commits.
- After P9 (Shadow DOM), do a full extension reload + visual smoke test
  before committing. Shadow DOM is the highest-risk item — if any
  binding silently breaks (TF select, clear, close), catch it now.
- If yfinance flakes during testing, don't paper over it — just retry
  and note it in the chat.
