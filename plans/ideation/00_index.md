# Architectural ideation — index

This folder is theory, not commitment. Written at the end of MVP (phases
1–7 done, 8 ahead) when the architecture is small enough to still
re-shape cheaply. Every doc below proposes alternatives that would mean
undoing parts of v0. Bring an open mind, but don't refactor anything
based on these alone — each idea is a *prompt for a real plan*, not a
plan itself.

## Files

| File | Topic | "Could undo…" |
|---|---|---|
| `01_brain_topology.md` | What shape the LLM call takes — one-shot stuffing vs tool-calling agent vs structured output | the entire `/analyze` request shape |
| `02_data_and_state.md` | Where OHLC, notes, and history live; caching; journaling foundations | yfinance, the in-memory `_State`, full-overwrite notes |
| `03_frontend_viz.md` | Extension internals (vanilla JS, panel widgets) and how chart visualization integrates | vanilla content.js, Path-1-only stance |
| `04_process_topology.md` | Whether a Python backend should exist at all — alternatives: pure-extension, local daemon, cloud | the entire FastAPI process |
| `05_undo_candidates.md` | Concrete pieces of current code most worth re-examining | inline list |
| `06_detector_expansion.md` | Additive — what ICT detectors to add next (EQH/EQL, PD/OTE, PDH/PDL, killzones, breakers, IFVG) | nothing; pure addition |
| `07_autonomous_trading.md` | Could HAL trade itself — architectures, risk gating, staged path from paper to live, honest failure-mode catalog | the whole "copilot, not agent" stance |

`06_detector_expansion.md` is *additive*, not an undo candidate.
`07_autonomous_trading.md` is the opposite — the largest possible
expansion of HAL's surface area and the one with the most asymmetric
downside. Both are worth reading; neither ships without a real plan.

## The single most important axis

If only one architectural choice gets revisited before v1, make it the
one in `01_brain_topology.md`: do you stay in "stuff everything into one
prompt" mode or move to tool-calling? Everything else (state, data,
frontend) is downstream of how the brain interacts with the world.

## Honest meta-note

Many of these ideas trade complexity for capability. HAL today fits in
one head — that's a real feature. A polished v0 you actually use beats a
beautiful v1 architecture you don't. Re-read these only when current HAL
*hurts* somewhere specific.
