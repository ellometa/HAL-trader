# Frontend and visualization

## Current shape

- Single-file vanilla JS content script. ~290 lines. No bundler, no
  service worker, no framework, no TS.
- Styles in `panel.css`, all class-prefixed `.hal-*` to avoid TV
  collisions. No Shadow DOM.
- Storage in `chrome.storage.local`. Per-chart history and TF.
- Visualization story: Pine Script standalone indicators planned for
  phase 8 P4b. Nothing rendered on the chart yet.

This is a deliberately small surface and it has paid off — phases 6
and 7 each took under a day. The cost: any UI feature with state more
complex than "list of messages" gets gnarly fast.

---

## Alt 1 — Stay vanilla, add Shadow DOM

Wrap the panel root in a Shadow DOM. CSS isolation in both directions:
TV's stylesheets can't bleed into HAL, HAL's can't bleed into TV.

Cost: ~30 lines. No new tooling.

Why it matters: TradingView pushes regular CSS overhauls. The day they
introduce a class named `.hal-context` (low probability but nonzero),
we hide-break. Shadow DOM cuts that off cleanly.

**Recommended.** Cheap, defensive. Phase 8 P9 already proposes it.

---

## Alt 2 — Adopt TypeScript (with esbuild, no React)

Just the type layer. Single `esbuild content.ts → content.js` step,
runs in <50ms. No JSX. No React.

**Pros**
- Catches the bugs you actually have (typos in event names, missing
  storage keys, the SSE event-type union staying exhaustive).
- Forces explicit shapes for the features payload — once typed, the
  Pine-paste format from `06_visualization` becomes a single source of
  truth between backend and extension.
- Tiny ecosystem footprint at this scale.

**Cons**
- Build step in the loop. Today you save a file and reload the
  extension; tomorrow you save, wait 80ms for esbuild, reload.
- One more concept for the project.

**Verdict.** Worth it once `content.js` exceeds ~600 lines OR multiple
features start sharing types (paste block + meta event + storage
shape). Today: 290 lines, no shared types yet. **Park.**

---

## Alt 3 — Adopt a framework

I've thought about this for ~5 minutes. The answer is no.

- React + Vite turns "vanilla content script" into "extension boilerplate
  with manifest manifest manifest." Triples LoC.
- Preact + htm — middle ground. ~80 lines of setup. Still feels heavy
  for one panel.
- Lit — promising for the Web Component shape, but locks you into the
  framework's data-binding model.

**The right framework for HAL is no framework.** Revisit if HAL grows a
multi-tab UI or a dashboard with more than ~5 distinct components.

---

## Alt 4 — Move the panel out of the content script

Today the panel DOM is injected directly into TV's page. Alternative:
an extension *side panel* (`chrome.sidePanel` API, MV3-native, Chrome
114+).

**Pros**
- Lives outside TV's DOM completely. No more `z-index: 2147483000`,
  no more TV style bleed worries, no more mousedown propagation hacks.
- Survives TV's own popup/modal/lightbox without interference.
- Built-in resize, drag, close.

**Cons**
- Side panel is page-scoped in different rules — pinning to a specific
  tab is awkward. Per-chart storage gets messier.
- Loses spatial proximity to the chart. Visual context of "answer next
  to the bar I'm looking at" is real.

**Verdict.** Try once. If it feels bad, revert. I suspect it'll feel
worse than the current overlay specifically because chart-adjacency
matters for trading-eye work. Park it.

---

## Visualization — beyond phase 8 P4b

Phase 8 already locks "Path 1 standalone Pine" as the visual rail. The
following expand that.

### Viz 1 — Paste-driven Pine companion (P4c)

Already drafted in conversation. Pine `input.text_area()` consumes a
small payload HAL emits at the bottom of every response. Drops in for
1:1 fidelity replay of any HAL call.

```
fvg_bull,80240.87,80327.75,1715211540,1715218740,mitigated
ob_bear,80407.6,80776.33,1715301240,0,unmitigated
```

~80 lines of Pine, ~10 lines in `prompts.py` to emit the block.
**Best paired with phase 9 journaling** — every logged analysis stays
replayable on the chart.

### Viz 2 — Mini-chart inside the panel

Render a tiny canvas chart of the queried symbol+TF in the panel
header, with detected zones overlaid. Uses Lightweight Charts (TV's
free embeddable lib).

**Pros**
- Doesn't depend on the user being on the right TV chart.
- Visual confirmation of what HAL "sees" even when they hopped to
  another tab.

**Cons**
- ~200 lines of new extension code + the lib bundle (~150KB).
- Duplicates what they're literally already looking at. Adds clarity
  in screenshots and journal replay, less so day-to-day.

**Verdict.** Skip for now. Revisit if journaling makes "review an old
HAL call without opening the chart" a frequent action.

### Viz 3 — Extension-drawn overlay on top of TV's chart

DOM overlay positioned exactly over the TV chart area, drawing
absolute-positioned divs at chart-coordinate price levels. Looks
seamless when it works. Bleeds disaster when TV resizes/rerenders.

This was originally Path 3 in the earlier visualization research and
was explicitly rejected as too brittle. Re-confirming that rejection
here. **Do not build.**

### Viz 4 — Export to multiple platforms

Treat the "zones data" as the canonical artifact, with renderers:
- Pine Script (already planned)
- thinkorswim's thinkScript
- NinjaScript
- A simple HTML+SVG renderer for the journal

Almost certainly YAGNI. You use one charting platform. If that ever
changes, build then. **Park.**

---

## Recommendation

Phase 8 polish stays as scoped (P4b Pine, P9 Shadow DOM, P4 robust
symbol detection). Add P4c as a separate work item once journaling
exists — it's the multiplier on phase 9, not on phase 8.

Don't TypeScript-ify yet. Don't framework. Don't side-panel.

The frontend is well-shaped for what HAL is. Most "improve the
frontend" instincts are actually "improve some other part of HAL"
in disguise.
