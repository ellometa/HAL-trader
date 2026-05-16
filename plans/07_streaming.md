# Phase 7 — End-to-End Streaming

## Goal

Tokens stream from Gemini through the backend to the extension UI,
appearing progressively in the panel. Replaces the non-streaming JSON
response from phase 4. Pattern: Gemini's streaming generator → SSE
from FastAPI → `fetch`-with-reader in the extension.

## Context (what exists going in)

- Backend `/analyze` returns JSON (phase 4 contract). We're replacing
  the response body for this endpoint.
- Extension currently does `await resp.json()` and renders `data.answer`
  in one shot. We're replacing that read path.
- Gemini's `google-genai` SDK supports streaming via
  `client.models.generate_content_stream(...)` returning an iterator
  of chunks where each chunk has `.text`.
- The phase 4 response also returned `features` and `current_price`.
  Decide where those go in a streaming world — see Q1.

## Deliverables

- Modify `backend/main.py`:
  - `/analyze` switches to `StreamingResponse` with
    `media_type="text/event-stream"`.
  - First event = a `meta` event containing `features`,
    `current_price`, `candles_used` as JSON. Subsequent events =
    `token` events with text chunks. Final event = `done`.
  - Add a separate `/analyze_blocking` endpoint that keeps the
    phase-4 JSON behavior, for curl-friendly debugging. (Cheap,
    optional, but useful.)
- Modify `extension/content.js`:
  - Replace `resp.json()` with a reader over `resp.body` that parses
    SSE frames and appends tokens to the current assistant message
    bubble as they arrive.
  - On the `meta` event, optionally render a small details
    expander showing detected features (useful for debugging).
  - On the `done` event, finalize the bubble (e.g. stop the
    "thinking…" indicator).

## SSE event format

```
event: meta
data: {"features": {...}, "current_price": 64321.5, "candles_used": 200}

event: token
data: {"text": "Looking at the recent "}

event: token
data: {"text": "bullish FVG at "}

...

event: done
data: {}
```

Each event terminated by a blank line. The `data:` payload must be
a single line — escape newlines in token text as `\\n` and unescape
on the client. (Or, simpler: only send text that doesn't contain
literal newlines, which Gemini's chunks typically don't — but the
escape is safer.)

**Tradeoff comment to include in `main.py`:** alternatives are
WebSockets and HTTP chunked plain text. SSE wins here because (a) it's
one-way which matches our needs, (b) the browser's `fetch` + reader
already supports it, (c) it auto-reconnects if we ever wrap it with
`EventSource` (we don't here, but the format is standard).

## Step-by-step prompt

Working dir `/Users/ellometa/code/Experimenting/HAL`. Phases 1–6 are
complete. The extension can talk to the backend and render single-shot
answers.

1. In `backend/main.py`:
   - Build the prompt and call
     `client.models.generate_content_stream(model="gemini-2.5-flash", contents=user_msg, config=...)`
   - Wrap in an async generator that yields SSE-formatted strings
     (`f"event: token\ndata: {json.dumps({'text': chunk.text})}\n\n"`).
     The first yielded value is the `meta` event built BEFORE the
     stream starts. The last is `event: done\ndata: {}\n\n`.
   - Return `StreamingResponse(gen(), media_type="text/event-stream")`.
   - Set headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no`
     (the latter is harmless and prevents some proxies from
     buffering; harmless on localhost).
   - Also: SSE through some browsers is sensitive to keepalives. For
     v0 localhost, ignore.
   - Add `/analyze_blocking` that does the phase-4 non-streaming
     behavior. Internally, share the prompt-building helpers — don't
     duplicate.
2. In `extension/content.js`:
   - Replace the response-handling block:
     ```js
     const resp = await fetch('http://localhost:8000/analyze', {...});
     const reader = resp.body.getReader();
     const decoder = new TextDecoder();
     let buffer = '';
     while (true) {
       const {value, done} = await reader.read();
       if (done) break;
       buffer += decoder.decode(value, {stream: true});
       // split on \n\n (SSE event boundary)
       const events = buffer.split('\n\n');
       buffer = events.pop();
       for (const ev of events) handleSSEEvent(ev);
     }
     ```
   - `handleSSEEvent(raw)`: parses `event:` and `data:` lines,
     dispatches to a handler per event type.
   - `meta`: stash features for the debug expander.
   - `token`: append `data.text` to the current bubble's textContent.
   - `done`: clear the thinking indicator.
   - Render the appended tokens via `textContent +=` (NOT
     `innerHTML`) to avoid any HTML injection from model output.
3. Test path A: with backend running, send a query from the
   extension. Confirm tokens appear progressively (not all at once).
4. Test path B: `curl -N http://localhost:8000/analyze -d '...'`
   should print events as they arrive.
5. STOP.

## Acceptance criteria

- Tokens visibly stream in the panel, one chunk at a time, within
  ~500ms of starting.
- `curl -N -X POST http://localhost:8000/analyze -H 'Content-Type: application/json' -d '...'`
  prints SSE events progressively (note the `-N` to disable curl
  buffering).
- `/analyze_blocking` still works for one-shot curl testing.
- Killing the backend mid-stream surfaces a visible error in the
  panel, not a half-complete bubble with no indication.
- No HTML injection — pasting a model response containing `<script>`
  shows it as literal text.

## Locked decisions

- **Features delivery:** first SSE `meta` event carries `features`,
  `current_price`, `candles_used`.
- **Streaming granularity:** raw token-by-token (no sentence
  buffering).
- **Abort button:** DEFERRED to phase 8 polish. Phase 7 ships
  without an abort UI.

## Out of scope

- No retry / no resume.
- No multi-turn conversation memory across messages. Each /analyze
  call is independent (consistent with prior phases).
- No support for tool-calling streams (Gemini's `function_call`
  chunks).
- No WebSocket alternative.
- No server-side cancellation when the client disconnects (FastAPI
  handles this reasonably by default).
