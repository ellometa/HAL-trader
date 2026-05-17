"""FastAPI app for HAL — POST /analyze (SSE), POST /analyze_blocking, GET /health.

Phase 7 swaps /analyze to a token-by-token Server-Sent Events stream while
keeping a non-streaming /analyze_blocking around for curl-friendly debugging.

Why SSE over WebSockets / chunked plain text: (a) one-way fits our shape,
(b) browser fetch + reader already speaks it, (c) the wire format is
standard enough that we could drop in EventSource later if useful.
"""
import json
import logging
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
    features, system_prompt, user_msg, current_price, candles_used = await _prepare(req)
    assert _state.client is not None
    client = _state.client

    async def gen() -> AsyncIterator[str]:
        meta: dict[str, Any] = {
            "features": features,
            "current_price": current_price,
            "candles_used": candles_used,
        }
        if debug:
            meta["_debug"] = {"system_prompt": system_prompt, "user_message": user_msg}
        yield _sse("meta", meta)

        try:
            stream = await client.aio.models.generate_content_stream(
                model=_MODEL,
                contents=user_msg,
                config=types.GenerateContentConfig(system_instruction=system_prompt),
            )
            async for chunk in stream:
                text = getattr(chunk, "text", None)
                if text:
                    yield _sse("token", {"text": text})
        except Exception as e:
            # Localhost-only — leaking the message is fine for debugging.
            log.exception("gemini stream failed")
            yield _sse("error", {"detail": f"Gemini error: {e}"})
            return

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
    features, system_prompt, user_msg, current_price, candles_used = await _prepare(req)
    assert _state.client is not None
    try:
        resp = await _state.client.aio.models.generate_content(
            model=_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini error: {e}") from e

    body: dict[str, Any] = {
        "answer": resp.text,
        "features": features,
        "candles_used": candles_used,
        "current_price": current_price,
    }
    if debug:
        body["_debug"] = {"system_prompt": system_prompt, "user_message": user_msg}
    return body
