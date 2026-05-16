"""Loads .env from repo root and exposes secrets as module-level constants.

Resolving .env relative to this file (not CWD) lets any script work
regardless of where it's invoked from.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_REPO_ROOT / ".env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "")
NOTION_ROOT_PAGE_ID = os.getenv("NOTION_ROOT_PAGE_ID", "")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY missing from .env. "
        f"Expected file: {_REPO_ROOT / '.env'}"
    )
