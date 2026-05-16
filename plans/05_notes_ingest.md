# Phase 5 — Notion → notes.md Ingestion

## Goal

A one-shot Python script that recursively walks a Notion root page,
pulls every text block from every descendant page, and writes the
combined content to `backend/notes.md` as plain markdown. The backend
(phase 4) already reads this file at startup; this phase just
populates it with real content.

## Context (what exists going in)

- Backend reads `backend/notes.md` at startup (phase 4).
- `NOTION_TOKEN` and `NOTION_ROOT_PAGE_ID` are env vars defined in
  `.env.example` since phase 1; user has not filled them in yet —
  they will when this phase starts.
- `backend/config.py` already loads them but may not currently raise
  on missing values (only `GEMINI_API_KEY` was required). The script
  should validate them itself.

## Deliverables

- `scripts/ingest_notion.py`      — runnable: `uv run python -m scripts.ingest_notion`
                                    or `python scripts/ingest_notion.py`
- Adds `notion-client` via `uv add 'notion-client~=2.2'`
- Updates `backend/notes.md` (gitignored, content overwritten on
  every run)

## Behavior

1. Validate `NOTION_TOKEN` and `NOTION_ROOT_PAGE_ID` from env. Fail
   with a clear message if either is missing.
2. Fetch the root page's metadata (for the title).
3. Recursively walk children: for each block, if it's a child page,
   recurse into it; otherwise, render the block as markdown.
4. Concatenate output as a single string with headings inserting page
   titles:
   ```
   # <root page title>
   <blocks...>

   ## <subpage 1 title>
   <blocks...>

   ### <sub-subpage>
   <blocks...>
   ```
   Heading depth = recursion depth + 1 (capped at h6).
5. Write to `backend/notes.md`. Print a one-line summary: pages
   visited, total characters written.

## Block → markdown mapping

Only handle the block types that show up in human-written notes.
Anything unhandled → skip with a comment-style line:
`<!-- skipped <type> block -->`. Don't crash on unknown types.

| Notion block type      | Markdown rendering                                    |
|------------------------|--------------------------------------------------------|
| `paragraph`            | rich-text → plain text, then blank line               |
| `heading_1`            | `# {text}`                                            |
| `heading_2`            | `## {text}`                                           |
| `heading_3`            | `### {text}`                                          |
| `bulleted_list_item`   | `- {text}`                                            |
| `numbered_list_item`   | `1. {text}` (don't bother numbering correctly — md renders fine) |
| `to_do`                | `- [ ] {text}` or `- [x] {text}`                      |
| `quote`                | `> {text}`                                            |
| `code`                 | fenced ``` block with language                        |
| `divider`              | `---`                                                 |
| `child_page`           | recurse (do NOT render as a block here)               |
| `toggle`               | render text + recurse into children                   |
| `callout`              | `> 💡 {text}` (preserve emoji if present)             |
| `image`                | `<!-- image: {url or caption} -->` (no embedding for v0) |

Rich-text → markdown: handle bold (`**...**`), italic (`*...*`), code
(`` `...` ``), links (`[text](url)`). Don't worry about colors or
backgrounds.

## Step-by-step prompt

Working dir `/Users/ellometa/code/Experimenting/HAL`. Phases 1–4
complete; backend is curl-testable. The user has `NOTION_TOKEN` and
`NOTION_ROOT_PAGE_ID` in `.env`.

1. SDK is **locked to `notion-client`**. Run `uv add 'notion-client~=2.2'`.
3. Implement `scripts/ingest_notion.py`. Structure:
   - `def get_block_children(client, block_id) -> list[block]` — handles
     pagination (`has_more` / `next_cursor`).
   - `def page_title(client, page_id) -> str`.
   - `def render_block(block, depth) -> str` — returns markdown for
     this block. Returns empty string for `child_page` (handled
     by walker).
   - `def render_rich_text(rt_array) -> str`.
   - `def walk_page(client, page_id, depth=0) -> str` — recursive.
   - `def main()`.
4. Be defensive: rate-limit Notion responses to ~3 req/s (a 100ms
   sleep between API calls is enough for v0). Print progress every N
   pages.
5. Test with the user's actual Notion workspace. Show the user the
   first 30 lines of `notes.md` and the total character count.
6. Restart the backend (`uvicorn ... --reload` will auto-pick it up,
   but `notes.md` is only read at startup, so a manual restart is
   required). Re-run a curl test from phase 4 with a question whose
   answer should rely on the notes content. Confirm the answer
   reflects the notes.

## Acceptance criteria

- `uv run python -m scripts.ingest_notion` exits 0, prints
  `wrote N pages, M chars to backend/notes.md`.
- `backend/notes.md` exists, non-empty, and renders sensibly when
  viewed in a markdown previewer.
- Backend restart + curl call: the model's answer references the
  user's notes (e.g. uses the user's specific terminology or
  references rules from the notes).
- `git status` does not show `backend/notes.md` (gitignored from
  phase 1).
- Re-running the script is idempotent: it overwrites cleanly and
  produces the same output if no Notion content changed.

## Open questions / decisions

- **Q1.** Token estimate after ingestion. If `notes.md` exceeds
  ~500k characters, stuffing it all into the system prompt will eat
  a meaningful chunk of context. We're explicitly NOT doing RAG in
  v0 — but flag the size to the user so they can decide whether to
  split.
- **Q2.** Should the backend reload `notes.md` on every request
  instead of at startup? Default: keep startup-only; add a
  `/reload-notes` POST endpoint as polish in phase 8 if it becomes
  painful.

## Out of scope

- No RAG, no chunking, no embeddings.
- No image OCR, no embed downloading.
- No incremental sync — full rewrite every run.
- No watching for changes / no cron.
- No database integration / no Postgres / no SQLite.
- Don't build a `notion_renderer/` package abstraction; one script
  file is fine.
