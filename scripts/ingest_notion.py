"""Notion → backend/notes.md ingestion.

One-shot script. Walks the configured root page recursively, renders every
supported block type as markdown, and writes the concatenated result to
`backend/notes.md`. The backend reads that file at startup (phase 4).

Run:
    uv run python -m scripts.ingest_notion

Re-runs are full overwrites — no incremental sync.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from notion_client import Client

from backend.config import NOTION_ROOT_PAGE_ID, NOTION_TOKEN

_NOTES_PATH = Path(__file__).resolve().parent.parent / "backend" / "notes.md"
_RATE_LIMIT_SLEEP = 0.1  # ~3 req/s ceiling. Notion's published limit is 3/s.


def _rich_text(rt_array: list[dict]) -> str:
    """Notion rich-text array → markdown inline string."""
    out: list[str] = []
    for rt in rt_array:
        text = rt.get("plain_text", "")
        if not text:
            continue
        ann = rt.get("annotations", {})
        if ann.get("code"):
            text = f"`{text}`"
        if ann.get("bold"):
            text = f"**{text}**"
        if ann.get("italic"):
            text = f"*{text}*"
        href = rt.get("href")
        if href:
            text = f"[{text}]({href})"
        out.append(text)
    return "".join(out)


def _get_children(client: Client, block_id: str) -> list[dict]:
    """Paginated block-children fetch."""
    results: list[dict] = []
    cursor: str | None = None
    while True:
        time.sleep(_RATE_LIMIT_SLEEP)
        kwargs: dict = {"block_id": block_id, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        resp = client.blocks.children.list(**kwargs)
        results.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return results


def _page_title(client: Client, page_id: str) -> str:
    time.sleep(_RATE_LIMIT_SLEEP)
    page = client.pages.retrieve(page_id=page_id)
    # The title property is named differently for workspace pages vs database
    # rows. For a plain page, "properties.title.title" holds the rich-text.
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title":
            return _rich_text(prop.get("title", [])) or "Untitled"
    return "Untitled"


def _render_block(block: dict) -> str:
    """Render a single block (no recursion into children). Returns '' for
    block types the walker handles itself (child_page) and for unsupported
    types where a skip-comment is more useful than empty output."""
    btype = block.get("type", "")
    data = block.get(btype, {})
    rt = data.get("rich_text", [])
    text = _rich_text(rt)

    if btype == "paragraph":
        return f"{text}\n" if text else "\n"
    if btype == "heading_1":
        return f"# {text}\n"
    if btype == "heading_2":
        return f"## {text}\n"
    if btype == "heading_3":
        return f"### {text}\n"
    if btype == "bulleted_list_item":
        return f"- {text}\n"
    if btype == "numbered_list_item":
        return f"1. {text}\n"
    if btype == "to_do":
        checked = data.get("checked", False)
        return f"- [{'x' if checked else ' '}] {text}\n"
    if btype == "quote":
        return f"> {text}\n"
    if btype == "code":
        lang = data.get("language", "") or ""
        return f"```{lang}\n{text}\n```\n"
    if btype == "divider":
        return "---\n"
    if btype == "callout":
        icon = data.get("icon") or {}
        emoji = icon.get("emoji", "💡") if icon.get("type") == "emoji" else "💡"
        return f"> {emoji} {text}\n"
    if btype == "toggle":
        return f"{text}\n" if text else ""
    if btype == "image":
        img = data
        url = (img.get("external") or img.get("file") or {}).get("url", "")
        caption = _rich_text(img.get("caption", []))
        label = caption or url or "image"
        return f"<!-- image: {label} -->\n"
    if btype == "child_page":
        return ""  # handled by walker
    return f"<!-- skipped {btype} block -->\n"


def _walk(client: Client, page_id: str, depth: int, stats: dict) -> str:
    """Recursive walk. depth 0 = root. Headings use depth+1, capped at h6."""
    stats["pages"] += 1
    title = _page_title(client, page_id)
    heading_level = min(depth + 1, 6)
    out: list[str] = [f"\n{'#' * heading_level} {title}\n\n"]

    children = _get_children(client, page_id)
    for block in children:
        btype = block.get("type", "")
        if btype == "child_page":
            child_id = block["id"]
            out.append(_walk(client, child_id, depth + 1, stats))
            continue
        out.append(_render_block(block))
        # Toggles can have nested blocks worth flattening into the output.
        if btype == "toggle" and block.get("has_children"):
            nested = _get_children(client, block["id"])
            for nb in nested:
                if nb.get("type") == "child_page":
                    out.append(_walk(client, nb["id"], depth + 1, stats))
                else:
                    out.append(_render_block(nb))

    if stats["pages"] % 5 == 0:
        print(f"  …{stats['pages']} pages walked", file=sys.stderr)
    return "".join(out)


def main() -> int:
    if not NOTION_TOKEN:
        print("error: NOTION_TOKEN missing from .env", file=sys.stderr)
        return 1
    if not NOTION_ROOT_PAGE_ID:
        print("error: NOTION_ROOT_PAGE_ID missing from .env", file=sys.stderr)
        return 1

    client = Client(auth=NOTION_TOKEN)
    stats = {"pages": 0}
    print(f"ingesting from root page {NOTION_ROOT_PAGE_ID}…", file=sys.stderr)
    content = _walk(client, NOTION_ROOT_PAGE_ID, depth=0, stats=stats)

    _NOTES_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {stats['pages']} pages, {len(content)} chars to {_NOTES_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
