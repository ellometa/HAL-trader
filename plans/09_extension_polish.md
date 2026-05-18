# Phase 9 — Extension polish + conversation memory

## Goal

Make HAL pleasant to *use*. Phase 8 sharpened answers; phase 9 sharpens
the surface around them — multi-turn context, abortable streams,
robust chart parsing, a Pine visual rail, and a defensive style
boundary.

## Prerequisite context

Phase 8 done:
- Detectors now include BOS/CHoCH + liquidity sweeps.
- OHLC caching cuts repeat-fetch latency.
- Panel exposes detected features under a debug expander.
- README reflects current reality.

Read before starting: `extension/content.js`, `extension/panel.css`,
`extension/manifest.json`, `backend/main.py` (for session_id wiring),
`plans/00_overview.md`.

---

## Scope (in order)

### P2. Conversation memory (per-tab, in-memory)

Every `/analyze` is currently independent. Multi-turn questions ("what
about the 1h?") fall flat.

**Backend changes (`backend/main.py`)**
- `AnalyzeRequest` gains optional `session_id: str | None`.
- Add module-level `_sessions: dict[str, list[Turn]]` where `Turn` is
  `{role, content, ts}`.
- On each call: if `session_id` present, prepend the last N turns to
  the `contents` argument of the Gemini call. N = 6 (3 user, 3
  assistant) — tune by feel.
- Eviction: per-session cap of 20 turns; sweep sessions inactive >1h
  on every call.
- Don't persist. Server restart wipes memory by design — this is
  *short-term* context, not journaling.

**Extension changes (`extension/content.js`)**
- On panel open, generate a UUID; store in `chrome.storage.local`
  keyed `hal/session/<chart-slug>`.
- Send `session_id` with every `/analyze` POST.
- "Clear chat" button also rotates the session_id.

**Trade-offs**
- Token cost rises with turn count. Flash is cheap; still cap N.
- Multi-tab on the same chart: same chart-slug → same session_id →
  shared context. Acceptable for v0.

**Acceptance**
- Ask "what's the structure on BTC 4h?", then ask "what about the
  1h?". HAL infers symbol from prior turn.
- "Clear chat" resets the panel *and* starts a fresh server-side
  session.

### P3. Abort button + visible thinking state

**Extension changes**
- Wrap the streaming fetch in `AbortController`.
- While streaming, the Send button becomes a Stop button (or add a
  separate one — pick one and commit).
- Thinking state: between submit and first `meta` event, show a
  spinner or animated dots in the bubble.
- On abort: gracefully close the stream, append "(stopped)" to the
  in-flight bubble, re-enable Send.

**Acceptance**
- Long answer mid-stream, click Stop → tokens stop flowing within a
  frame, bubble closed cleanly, panel ready for next input.
- No console errors on abort.

### P4. Robust symbol detection on TradingView

The current path: `?symbol=` URL param, fall back to `document.title`
regex. Per `plans/ideation/05_undo_candidates.md`, the title fallback
hasn't fired in real use. Either harden it or delete it.

**Approach**
- Test across ≥5 chart types: spot crypto, perp futures, stock,
  index, forex pair, futures with month code.
- For each, record the URL pattern and the parsed result.
- If `?symbol=` always wins: delete `symbolFromTitle()` (ideation
  item 5). Surface a clean "couldn't detect symbol" UI error if
  parsing fails.
- If `?symbol=` misses on some chart types: extend the parser; keep a
  *narrower* title fallback that handles only those cases.

**Acceptance**
- A short test matrix (chart type → parsed symbol) lives at the top
  of `content.js` as a comment.
- Manual spot-check on each chart type returns the expected symbol.
- Detection failure produces a visible, actionable panel error — not
  a silent wrong-symbol call.

### P4b. Pine Script FVG / OB highlighter

Standalone Pine v6 indicators that mirror `backend/ict/fvg.py` and
`backend/ict/order_blocks.py`. User pastes them once into TradingView's
Pine editor and adds to chart. HAL's zones and the chart visuals stay
in sync because the detection rule is *identical*.

**Files to add**
- `pine/hal_fvg.pine`
- `pine/hal_ob.pine`

**Rules**
- Detection logic ported 1:1 from the Python implementation. If a
  Python edge case isn't captured in Pine, port it; if it can't be
  ported (Pine limitation), document why.
- Inputs:
  - FVG: bullish/bearish/mitigated toggles, color pickers.
  - OB: bullish/bearish/mitigated toggles, swing_n input, colors.
- Out of scope: extension-driven Pine (the "Path 3" idea). User adds
  the indicator once, manually.

**Acceptance**
- On BTC 4h, the Pine FVG count matches HAL's reported count for the
  same lookback window. Same for OB.
- Mitigation states render visually (e.g. unmitigated = solid,
  mitigated = hatched/transparent).

### P9. Shadow DOM for the extension panel

TradingView ships regular CSS overhauls. Wrap the HAL panel root in a
Shadow DOM to isolate styles in both directions.

**Changes**
- `injectPanel()` creates a Shadow root attached to a host element.
- All HAL DOM (panel header, messages, input) lives inside the shadow
  root.
- `panel.css` content is injected as a `<style>` inside the shadow
  root rather than loaded from `manifest.json` content_scripts CSS.
- Mouse event stopPropagation handlers stay (they target the host
  element).

**Trade-offs**
- `chrome.storage.local` access is unaffected.
- Slight re-plumbing for any code that queries the document directly
  for HAL elements (none, currently).

**Acceptance**
- Toggle TradingView themes (light/dark) — HAL panel styling
  unchanged.
- Inject a stress-test global stylesheet (`* { color: red }`) via
  devtools — HAL text stays readable.

---

## Out of scope for phase 9

- Persistent journaling, SQLite, server-side history — separate plan.
- Detector toggles (P13), eval harness (P14), prompt tuning (P10).
- TypeScript migration, framework adoption — see
  `plans/ideation/03_frontend_viz.md` for the "park" decision.
- launchd auto-start, `hal serve` CLI — see
  `plans/ideation/04_process_topology.md` (queue as P17 if desired).

## Suggested order

1. P2 first. Conversation memory is the single biggest UX upgrade.
2. P3 second. Naturally pairs with P2 — once turns matter, aborting
   matters.
3. P4 third. Cheap, removes a phantom failure mode.
4. P9 fourth. Defensive; do it before P4b so the panel's stable
   while you're testing Pine in parallel TV tabs.
5. P4b last. Independent of the extension code, separate concern.

## Definition of done

- All five items shipped, committed, pushed.
- Manual end-to-end: open BTC 4h, ask three follow-up questions
  in sequence, abort one mid-stream, switch to AAPL chart, ask a
  fresh question, verify Pine FVG/OB counts match HAL's reported
  features.
- The panel survives a TV CSS reload (shadow DOM isolation holds).
