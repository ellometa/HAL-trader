# Process topology — does HAL need a backend at all?

## Current shape

- Python FastAPI process on `127.0.0.1:8000`.
- Started manually with `uv run uvicorn …` in a terminal.
- Holds notes in memory, holds Gemini client, holds nothing else.
- Chrome extension content script POSTs to it.

Three processes if you count Chrome: the OS, Chrome, uvicorn. Manual
lifecycle for uvicorn.

The terminal-uvicorn ritual is the single most "this feels janky" part
of daily HAL use. Every solution below partially addresses it.

---

## Alt 1 — Make uvicorn auto-start

Smallest possible change. macOS launchd, plist file, runs at login,
restarts on crash. Linux: systemd user unit. Windows: scheduled task.

~30 lines of XML/yaml. Zero code change. Adds an `install` script.

**Pros**
- The friction disappears immediately.
- Backend is always there. Panel "just works."

**Cons**
- Have to remember to update the daemon when changing branch / poetry
  env (the launchd plist references a specific Python binary).
- Forgetting to restart on prompt changes becomes the new annoyance —
  swap one papercut for another.
- Adds an OS-specific install step.

**Verdict.** Highest-ROI single change. Should ship in phase 8 polish.
Phase 5 reload endpoint (P5) removes the "forget to restart" problem.

---

## Alt 2 — Bundle the backend as a CLI

`hal serve` starts uvicorn. `hal ingest` re-ingests notes. `hal stats`
prints recent activity once journaling exists.

Just a thin Click/Typer wrapper around the existing entrypoints. Doesn't
change the architecture; changes the *interface*.

Pair with Alt 1: launchd runs `hal serve`. Easier to read in the plist,
easier to invoke ad hoc.

**Recommended companion** to Alt 1. ~half a day.

---

## Alt 3 — Eliminate the backend, run everything in the extension

The radical option. Pure browser implementation:

- OHLC fetched directly by the content script from Binance/yfinance.
  (yfinance has no public CORS-friendly API — would need a different
  equity source, e.g. Yahoo's `query1.finance.yahoo.com` which works
  from extensions thanks to `host_permissions`.)
- Detectors ported to JavaScript. ~300 lines, mechanical port. Could
  also use Pyodide if you wanted to keep Python — but that's a 10MB
  WASM blob to download.
- Gemini called directly from the extension via the JS SDK. **API key
  in extension storage** — sketchy but it's a personal tool.
- Notes ingested by a separate desktop script that writes to
  `chrome.storage.local` via the extension's exposed messaging API.
  Awkward.

**Pros**
- One process. One thing to install. One thing to update.
- No CORS gymnastics.
- Offline-friendly (modulo Gemini calls and OHLC fetches).

**Cons**
- API key exposure. Anyone with extension access can extract it. Not
  the end of the world (Gemini Flash is cheap, billing alerts cap
  losses) but smells bad.
- Python-only tooling becomes JavaScript-only. yfinance has no
  replacement; rolling your own equity OHLC source is real work.
- Notes ingestion via Notion API needs CORS proxies or a separate
  server — partial defeat of the whole point.
- pytest goes away. Vitest or Bun's test runner replaces it.
- The day you want server-side state (journaling, multi-device), you
  rebuild the backend you just deleted.

**Verdict.** Tempting in theory. **Don't.** The journaling phase (9)
will need server state inside 2 weeks of v0 use. Burning the backend
now means rebuilding it then.

---

## Alt 4 — Backend in a non-Python language

Rust + Axum, Go + Echo, Node + Fastify. All can do what HAL's backend
does today in fewer lines and with no Python venv pain.

**Pros**
- Single static binary. `./hal-server` and done. No `uv run`, no
  Python version skew, no virtualenv.
- Faster startup (sub-100ms vs uvicorn's ~1s).

**Cons**
- pandas / numpy don't exist there. Detectors get rewritten — ~500
  lines in any of the three. Tests get rewritten too.
- google-genai SDK is best in Python today. JS is decent. Rust/Go: roll
  your own HTTP client against the REST API.
- The thing HAL is doing — pandas-based analysis — is exactly Python's
  home court.

**Verdict.** No. Python won this round. Revisit if HAL stops being
pandas-shaped (e.g. moves to pure RPC against a remote detector
service).

---

## Alt 5 — Backend in the same Chrome extension via Native Messaging

Hybrid: extension uses Chrome's Native Messaging API to spawn a local
Python helper on-demand. No HTTP. No port. No CORS.

**Pros**
- Lifecycle managed by Chrome. Extension dies, helper dies.
- No need for a daemon.
- No port collisions if you ever run two TV browsers.

**Cons**
- Native Messaging requires a manifest installed in a fixed system
  path (`~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`).
  More install ceremony, not less.
- One Python process per Chrome instance. Cold-start every time the
  extension reloads. ~1s extra latency on first query.
- Curl-debuggability gone.

**Verdict.** Genuinely cool, definitely worse than current setup for
HAL specifically. The curl-debugging story matters during phase 9.

---

## Alt 6 — Cloud-host the backend

Free Render / Fly / Modal instance. Backend reachable at
`https://hal.<you>.app`. Extension talks to it.

**Pros**
- No local lifecycle problem at all.
- Available from any browser / device.
- Logs persisted off-machine.

**Cons**
- Latency: 50–200ms vs ~5ms localhost. Compounds with Gemini's own
  latency.
- Notes upload becomes a real flow (HTTP API or rebuild from scratch
  on the server every ingest).
- Now you're a cloud operator. Cert renewal, deploys, dependency
  upgrades, secrets handling. For a personal tool. **Big no.**
- Anyone with the URL can hit it — auth becomes mandatory.

**Verdict.** Don't. Reconsider only if HAL becomes a thing you'd let
others use.

---

## Recommendation

Phase 8 should pick up Alt 1 + Alt 2 as a polish item (currently not
listed — file as **P17**: "install as a launchd service via `hal serve`
CLI"). That single change kills the daily-friction problem without
touching the architecture.

Everything else: park unless the situation changes.

The current localhost-FastAPI shape is right for HAL today. Keep it.
