# HAL

ICT-aware chat overlay for TradingView. Local-only personal tool.

## Quick start

```bash
# install uv if you don't have it
brew install uv

# install deps (creates .venv automatically)
uv sync

# copy env template and paste your key
cp .env.example .env
# edit .env -> set GEMINI_API_KEY=...

# smoke test
uv run python -m backend.hello
```

Architecture, build phases, and design decisions live in `plans/`.
