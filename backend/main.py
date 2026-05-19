"""FastAPI app for HAL — POST /analyze (SSE), POST /analyze_blocking, GET /health.

Phase 7 swaps /analyze to a token-by-token Server-Sent Events stream while
keeping a non-streaming /analyze_blocking around for curl-friendly debugging.

Why SSE over WebSockets / chunked plain text: (a) one-way fits our shape,
(b) browser fetch + reader already speaks it, (c) the wire format is
standard enough that we could drop in EventSource later if useful.
"""
import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import types
from pydantic import BaseModel

from backend.config import GEMINI_API_KEY
from backend.ict.detector import detect_all
from backend.ohlc import TimeframeError, fetch_ohlc
from backend.prompts import build_system_prompt, build_user_message

_NOTES_PATH = Path(__file__).parent / "notes.md"
_MODEL = "gemini-2.5-flash"

log = logging.getLogger("hal")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class _State:
    notes: str = ""
    client: genai.Client | None = None


_state = _State()

# Per-session conversation memory. Personal tool, no auth — session_id is
# generated client-side and treated as authoritative. Features payloads
# are *not* stored — they'd be stale by the next turn; only question/answer
# text replays.
_sessions: dict[str, list[dict[str, Any]]] = {}
_SESSION_TURN_CAP = 20            # total turns per session (user + assistant)
_SESSION_IDLE_TIMEOUT = 3600.0    # seconds before idle session is dropped
_HISTORY_TURNS_TO_SEND = 6        # last N turns prepended to Gemini contents


def _evict_stale_sessions(now: float) -> None:
    stale = [
        sid for sid, turns in _sessions.items()
        if turns and now - turns[-1]["ts"] > _SESSION_IDLE_TIMEOUT
    ]
    for sid in stale:
        del _sessions[sid]


def _session_contents(session_id: str | None, user_msg: str) -> Any:
    """Build the Gemini ``contents`` arg.

    Without a session, return the single-string user message (preserves
    phase 7 behavior). With a session, prepend up to ``_HISTORY_TURNS_TO_SEND``
    prior turns as Content objects, then the new user turn.
    """
    if not session_id:
        return user_msg
    history: list[dict[str, Any]] = []
    for turn in _sessions.get(session_id, [])[-_HISTORY_TURNS_TO_SEND:]:
        role = "user" if turn["role"] == "user" else "model"
        history.append({"role": role, "parts": [{"text": turn["text"]}]})
    history.append({"role": "user", "parts": [{"text": user_msg}]})
    return history


def _record_turns(session_id: str | None, query: str, assistant_text: str) -> None:
    """Store the bare question + answer pair for replay. Features payload
    deliberately excluded — it'd be stale by the next call."""
    if not session_id or not assistant_text:
        return
    now = time.time()
    session = _sessions.setdefault(session_id, [])
    session.append({"role": "user", "text": query, "ts": now})
    session.append({"role": "assistant", "text": assistant_text, "ts": now})
    if len(session) > _SESSION_TURN_CAP:
        del session[: len(session) - _SESSION_TURN_CAP]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Notes loaded once at startup; restart server to pick up changes.
    # Phase 8 polish item: /reload-notes endpoint.
    if _NOTES_PATH.exists():
        _state.notes = _NOTES_PATH.read_text(encoding="utf-8")
        log.info("loaded notes.md (%d chars)", len(_state.notes))
    else:
        log.info("no notes.md found — using placeholder until phase 5 ingestion")
    _state.client = genai.Client(api_key=GEMINI_API_KEY)
    yield


app = FastAPI(lifespan=lifespan)

# CORS. MV3 content-script fetches use the *page's* origin, not the
# extension's, so we need to allow tradingview.com explicitly in addition
# to chrome-extension://* (used if/when we add a background fetch) and
# localhost (for curl / local UIs). Backend is bound to 127.0.0.1 — not
# reachable from the public internet — so this is safe.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=(
        r"^(chrome-extension://.*"
        r"|http://localhost(:\d+)?"
        r"|https://(www\.)?tradingview\.com)$"
    ),
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class AnalyzeRequest(BaseModel):
    symbol: str
    timeframe: str
    query: str
    session_id: str | None = None


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


async def _prepare(req: AnalyzeRequest) -> tuple[dict[str, Any], str, str, float, int]:
    """Shared work between /analyze and /analyze_blocking: fetch candles,
    run detectors, build prompts. Returns (features, system_prompt,
    user_msg, current_price, candles_used)."""
    try:
        df = await fetch_ohlc(req.symbol, req.timeframe)
    except TimeframeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        # Unknown symbol, no-data, upstream 4xx — all surface as 400 here.
        raise HTTPException(status_code=400, detail=str(e)) from e

    features = detect_all(df)
    current_price = float(df["close"].iloc[-1])
    system_prompt = build_system_prompt(_state.notes)
    user_msg = build_user_message(
        req.symbol, req.timeframe, current_price, features, req.query
    )
    return features, system_prompt, user_msg, current_price, len(df)


def _sse(event: str, payload: dict[str, Any]) -> str:
    # SSE data lines must be single-line — json.dumps without indent already
    # ensures no embedded raw newlines (they're escaped to \n in the JSON).
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


@app.post("/analyze")
async def analyze(req: AnalyzeRequest, debug: int = 0) -> StreamingResponse:
    _evict_stale_sessions(time.time())
    features, system_prompt, user_msg, current_price, candles_used = await _prepare(req)
    assert _state.client is not None
    client = _state.client
    contents = _session_contents(req.session_id, user_msg)

    async def gen() -> AsyncIterator[str]:
        meta: dict[str, Any] = {
            "features": features,
            "current_price": current_price,
            "candles_used": candles_used,
        }
        if debug:
            meta["_debug"] = {"system_prompt": system_prompt, "user_message": user_msg}
        yield _sse("meta", meta)

        accumulated: list[str] = []
        try:
            stream = await client.aio.models.generate_content_stream(
                model=_MODEL,
                contents=contents,
                config=types.GenerateContentConfig(system_instruction=system_prompt),
            )
            async for chunk in stream:
                text = getattr(chunk, "text", None)
                if text:
                    accumulated.append(text)
                    yield _sse("token", {"text": text})
        except asyncio.CancelledError:
            # Client disconnected mid-stream (P3 abort). Persist the partial
            # so the next turn carries the context the user already saw.
            _record_turns(req.session_id, req.query, "".join(accumulated))
            raise
        except Exception as e:
            # Localhost-only — leaking the message is fine for debugging.
            log.exception("gemini stream failed")
            yield _sse("error", {"detail": f"Gemini error: {e}"})
            return

        _record_turns(req.session_id, req.query, "".join(accumulated))
        yield _sse("done", {})

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Harmless on localhost; prevents reverse-proxy buffering elsewhere.
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/analyze_blocking")
async def analyze_blocking(req: AnalyzeRequest, debug: int = 0) -> dict[str, Any]:
    """Phase-4 non-streaming behavior, kept for curl-friendly debugging."""
    _evict_stale_sessions(time.time())
    features, system_prompt, user_msg, current_price, candles_used = await _prepare(req)
    assert _state.client is not None
    contents = _session_contents(req.session_id, user_msg)
    try:
        resp = await _state.client.aio.models.generate_content(
            model=_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini error: {e}") from e

    _record_turns(req.session_id, req.query, resp.text or "")
    body: dict[str, Any] = {
        "answer": resp.text,
        "features": features,
        "candles_used": candles_used,
        "current_price": current_price,
    }
    if debug:
        body["_debug"] = {"system_prompt": system_prompt, "user_message": user_msg}
    return body
