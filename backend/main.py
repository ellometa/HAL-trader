"""FastAPI app for HAL — POST /analyze, GET /health.

Non-streaming in phase 4. Phase 7 swaps the response body for an SSE
stream while keeping the same request shape.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
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


@app.post("/analyze")
async def analyze(req: AnalyzeRequest, debug: int = 0) -> dict[str, Any]:
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

    assert _state.client is not None
    try:
        # `client.aio.*` is the async variant — keeps us from blocking the
        # event loop on the Gemini round-trip.
        resp = await _state.client.aio.models.generate_content(
            model=_MODEL,
            contents=user_msg,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )
    except Exception as e:
        # Localhost-only deployment — leaking the message is fine for debugging.
        raise HTTPException(status_code=500, detail=f"Gemini error: {e}") from e

    body: dict[str, Any] = {
        "answer": resp.text,
        "features": features,
        "candles_used": len(df),
        "current_price": current_price,
    }
    if debug:
        body["_debug"] = {"system_prompt": system_prompt, "user_message": user_msg}
    return body
