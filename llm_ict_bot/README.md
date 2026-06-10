# LLM + ICT Hybrid Paper-Trading Bot — EUR/USD

A from-scratch build (independent of the rest of this repo): deterministic ICT/SMC
detectors produce a point-in-time multi-timeframe context, an LLM makes the trade
decision, a validator enforces the 1:2 reward:risk contract, and a 1-minute-resolution
paper simulator executes with spread-only costs and risk circuit breakers.

The decision layer is provider-pluggable (`LLM_PROVIDER` in the config cell):
**Ollama** (`llama3.1:8b`) honors the original local-only/no-paid-API spec;
**Gemini** (`gemini-2.5-flash-lite`) is a documented temporary exception, adopted
because a 16GB M4 sustains only ~35-45s per local 8B call while the Gemini API does
~1-2s — reasoning quality and wall-clock both matter for multi-month walk-forwards.

## Deliverable

**[`bot.ipynb`](bot.ipynb)** — runnable end-to-end, top to bottom. Two modes via
`RUN_MODE` in the config cell: `"walk_forward"` (anchored expanding-window backtest)
and `"live_forward"` (strict arrival-order replay of the newest bars).

`bot_src.py` is the jupytext percent-format source the notebook is generated from
(`jupytext --to ipynb bot_src.py -o bot.ipynb`); edit there, regenerate, re-run.
The `part*.py` files are the same source split into review-sized sections.

## Requirements

- Python 3.11+ with pandas, numpy, pyarrow, requests, matplotlib, jinja2
  (the repo `.venv` has all of these)
- The 1m EUR/USD Parquet store at `../data/eurusd_1m.parquet`
- One LLM provider:
  - Gemini: `export GEMINI_API_KEY=...` (aistudio.google.com/apikey), or put the key
    in a git-ignored `llm_ict_bot/.gemini_key` file (env var wins if both are set), or
  - Ollama: local server with `ollama pull llama3.1:8b`, plus `LLM_PROVIDER="ollama"`

Run headless with:

```sh
cd llm_ict_bot
GEMINI_API_KEY=... MPLBACKEND=Agg ../.venv/bin/jupyter-nbconvert \
  --to notebook --execute --inplace --ExecutePreprocessor.timeout=-1 bot.ipynb
```

Without the store or any LLM provider the notebook still executes end-to-end — a seeded
synthetic-data smoke test with a deterministic mock LLM exercises every code path
(parser, validator retry, confidence gate, cache replay, circuit breakers), and the
reporting cells fall back to those results.

## Outputs (under `runs/`, git-ignored)

- `reports/report_<run-id>.html` — summary stats (LLM vs rule-only vs buy-and-hold),
  equity/drawdown/trade plots, skip counts, circuit-breaker halts, fold table
- `reports/trades_<strategy>_<run-id>.csv` — full trade logs incl. LLM confidence + reasoning
- `logs/llm_calls_<run-id>.jsonl` — audit trail of every LLM call (raw prompt, raw
  response, parsed plan, validation verdict, latency, cache hit/miss)
- `cache/` — LLM responses keyed by context hash (re-runs replay from disk)

## Honest-comparison policy

The rule-only ICT baseline trades the *same* mechanical confluence the LLM is asked
to judge (liquidity sweep → structure shift → premium/discount filter) on the same
out-of-sample window. If the LLM does not beat it, the report says so plainly.
