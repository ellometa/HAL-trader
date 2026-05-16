# Phase 6 — Chrome Extension MVP

## Goal

A Manifest V3 Chrome extension that injects a floating button onto
TradingView tabs. Clicking the button opens a side panel with a text
input and conversation area. The extension reads the current chart's
symbol and timeframe from the TradingView URL/DOM, POSTs to
`http://localhost:8000/analyze`, and renders the JSON response.
Streaming is NOT in this phase — that's phase 7. Form follows function.

## Context (what exists going in)

- Backend at `http://localhost:8000/analyze` is working and returns
  the JSON contract from phase 4 (`{answer, features, candles_used,
  current_price}`).
- CORS already allows `chrome-extension://*` and
  `http://localhost:*`.
- No extension code exists yet.

## TradingView URL / DOM facts to confirm before coding

The extension needs to identify the active chart's **symbol** and
**timeframe**. Possible sources:

1. **URL**: e.g. `https://www.tradingview.com/chart/<chartId>/?symbol=BINANCE%3ABTCUSDT`
   — symbol is in the query string, sometimes. Not always populated
   when the user switches charts client-side.
2. **DOM**: the symbol shows up in `[data-name="legend-source-title"]`
   or similar; the timeframe in the top toolbar. Class names are
   obfuscated and change across TV redeploys — selectors WILL rot.
3. **`document.title`**: e.g. `BTCUSDT 1h chart — Crypto.com Exchange — TradingView`.
   Most stable of the three. Parse with a regex.

**Recommendation:** start with `document.title` parsing, fall back to
URL query string, fall back to a single DOM selector. Log to console
on each method so it's debuggable when TV changes things.

## Deliverables

```
extension/
  manifest.json         (MV3)
  content.js            (injected into TradingView pages; renders UI)
  panel.css             (styles for the floating button + panel)
  panel.html            (the panel structure — loaded as a string in content.js or inlined)
  icons/icon-128.png    (any placeholder; user can replace)
  icons/icon-48.png
  icons/icon-16.png
```

For v0 simplicity, render everything via `content.js` injecting DOM
nodes — no `panel.html` file, no Shadow DOM. If style bleed is bad,
upgrade to Shadow DOM in polish.

## manifest.json shape

```json
{
  "manifest_version": 3,
  "name": "HAL — TradingView assistant",
  "version": "0.1.0",
  "description": "ICT-aware chat overlay for TradingView.",
  "icons": {"16": "icons/icon-16.png", "48": "icons/icon-48.png", "128": "icons/icon-128.png"},
  "content_scripts": [
    {
      "matches": ["https://www.tradingview.com/*"],
      "js": ["content.js"],
      "css": ["panel.css"],
      "run_at": "document_idle"
    }
  ],
  "host_permissions": ["http://localhost:8000/*"],
  "permissions": ["storage"]
}
```

`host_permissions` for `localhost:8000` is what lets `fetch()` from
the content script hit the backend without being blocked by CORS in
some edge cases. (Most local-dev setups don't need it once the server
sets CORS correctly, but including it is belt-and-suspenders.)

## UX (minimum viable)

- Floating circular button, bottom-right, fixed position, z-index
  very high (TradingView uses ~9999 in spots; use 2147483000).
- Click → toggles a panel anchored to the right edge, ~380px wide,
  full height minus margins. Panel has:
  - Header: "HAL" + close button + a small label showing the
    currently-detected `symbol` and `timeframe` (auto-updates every
    few seconds; this also reassures the user the detection is
    working).
  - Scrollable conversation area.
  - Footer: textarea + send button. Enter sends, Shift+Enter newlines.
- On send: append the user's message bubble; show a "thinking…"
  placeholder; POST to backend; replace placeholder with `answer`.
- On error: render the error message in red within the conversation,
  don't alert().
- **Conversation persistence:** save each message into
  `chrome.storage.local` under a key derived from
  `chrome.tabs.Tab.id` (content script can ask its tab id via
  `chrome.runtime.sendMessage` to a tiny background helper — or, for
  v0 simplicity, hash `location.href`'s chart id as the key). Restore
  on panel open. Cap stored history per key to last 50 messages to
  prevent unbounded growth. Add a "Clear chat" button in the header
  that purges the storage entry.

## Symbol/timeframe detection logic

```
function detectSymbolAndTimeframe() {
  // 1. document.title — most stable
  //    e.g. "BTCUSDT 1h chart - …"
  //    Regex: /^([A-Z0-9:.]+)\s+(\d+[mhDWM]|\d+min|\d+h)\b/
  // 2. URL ?symbol=BINANCE:BTCUSDT
  // 3. fallback to last-seen, or null
  // strip exchange prefix (everything before ':') before returning
}
```

Run this on page load AND on a `MutationObserver` watching
`document.title`. Cache last value. Send the cached value with each
request.

Normalize the timeframe to the backend's set
(`1m, 5m, 15m, 1h, 4h, 1D, 1W`). Map TV's "60" → "1h", "D" → "1D",
etc. Put the mapping in one small function.

## Step-by-step prompt

Working dir `/Users/ellometa/code/Experimenting/HAL`. Backend is
running on `localhost:8000` and confirmed working via curl from
phase 4.

1. Create the `extension/` directory and the files listed above.
   For icons, generate trivial PNGs (single-color squares are fine)
   or have the user supply them.
2. Write `manifest.json` exactly as shown above.
3. Write `panel.css`: floating button + panel layout. Use a CSS
   prefix like `.hal-*` on every class to avoid collisions with
   TradingView's own styles. Don't import any fonts.
4. Write `content.js`:
   - Wait for `document.readyState === 'complete'`.
   - Inject the button.
   - Implement `detectSymbolAndTimeframe()` and the
     `MutationObserver` for `document.title`.
   - Implement the panel show/hide.
   - Implement `sendQuery(query)`:
     ```js
     const {symbol, timeframe} = detectSymbolAndTimeframe();
     const resp = await fetch('http://localhost:8000/analyze', {
       method: 'POST',
       headers: {'Content-Type': 'application/json'},
       body: JSON.stringify({symbol, timeframe, query}),
     });
     const data = await resp.json();
     renderAnswer(data.answer);
     ```
   - On error: catch, render `data.detail || error.message`.
5. Load the unpacked extension in Chrome:
   - Open `chrome://extensions`
   - Enable Developer Mode
   - Load Unpacked → select `HAL/extension/`
6. Open `https://www.tradingview.com/chart/?symbol=BINANCE:BTCUSDT`
   and confirm the button appears, the panel opens, the
   symbol+timeframe label updates correctly, and a sample query gets
   a real answer.
7. STOP. Have the user test 2–3 questions and at least one chart
   switch to confirm the symbol/timeframe detection survives
   client-side navigation.

## Acceptance criteria

- Extension loads without errors in `chrome://extensions` (no red
  errors on the extension card).
- Button appears on every TradingView chart page.
- Panel opens and closes.
- The symbol/timeframe label in the header reflects the actual
  chart, including after switching charts without a full reload.
- A real question gets a real answer in <10s. No CORS errors in
  DevTools console.
- Error path (e.g. kill the backend and send a query) renders a
  visible error message in the conversation, not a silent failure.

## Locked decisions

- **Styles:** CSS class prefixes (`.hal-*`). No Shadow DOM in v0.
- **Conversation persistence:** YES, via `chrome.storage.local`,
  keyed per-chart, capped at 50 messages, with a "Clear chat" button.
- **Symbol format:** strip exchange prefix in the extension; backend
  receives bare symbol (e.g. `BTCUSDT`, not `BINANCE:BTCUSDT`).

## Out of scope

- No streaming (phase 7).
- No persistence / no history.
- No keyboard shortcut to open the panel.
- No popup / no options page.
- No theme toggle. Default to dark — TradingView is dark.
- No service worker / no background script. Content-script-only.
- No tests (manual verification only).
