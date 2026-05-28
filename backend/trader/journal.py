"""Append-only decision journal (JSONL).

One JSON object per line, one line per (symbol, cycle) decision. Chosen
over sqlite because the access pattern is append + full-scan-replay, the
records are nested, and a human (or `jq`) being able to read the raw file
during an incident is worth more than query speed at this scale.

Every record carries the *exact inputs* that produced the decision — the
features snapshot and the prompt text — so any trade can be traced back to
the precise geometry and wording that caused it. This is the single most
important audit property for a thing that trades on its own: "why did it do
that" must always be answerable from the file alone.

The journal is append-only by discipline (we only ever open in 'a' mode)
and lives under the gitignored ``data/`` dir.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "journal.jsonl"


class Journal:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str, separators=(",", ":"))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def tail(self, n: int) -> list[dict[str, Any]]:
        return self.read_all()[-n:]
