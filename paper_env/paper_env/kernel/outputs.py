"""Collects cell outputs: scrubs secrets, caps what goes to the DB, keeps the full text in a file artifact."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..security import Scrubber

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MAX_ITEM_BYTES = 32 * 1024  # a single mime item bigger than this never goes into the DB
_HOLD_BACK = 256  # chars kept unflushed so a secret split across chunks is still seen whole
_FLUSH_AT = 8192


def strip_ansi(s: str) -> str:
    return ANSI.sub("", s)


class OutputRecorder:
    def __init__(self, scrubber: Scrubber, artifact_path: Path, max_db_bytes: int, max_total_bytes: int):
        self.scrubber = scrubber
        self.artifact_path = artifact_path
        self.max_db, self.max_total = max_db_bytes, max_total_bytes
        self._buf: dict[str, str] = {"stdout": "", "stderr": ""}
        self._db: list[dict[str, Any]] = []
        self._db_bytes = 0
        self.total_bytes = 0
        self._raw_bytes = 0
        self.truncated = False
        self.limit_exceeded = False
        self._file = None
        self._all: list[dict[str, Any]] = []  # held in memory only until the first truncation

    # ---- streams (line-buffered so secrets spanning chunks are caught) ----
    def stream(self, name: str, text: str) -> list[str]:
        """Returns scrubbed text ready to emit (possibly empty while a partial line is buffered)."""
        if self.limit_exceeded:
            return []  # cheap drop: no scrubbing/regex work while a runaway cell floods us
        self._raw_bytes += len(text)
        if self._raw_bytes > self.max_total:
            self._trip_limit()
            return []
        buf = self._buf[name] + text
        out = ""
        if "\n" in buf:
            head, _, tail = buf.rpartition("\n")
            out, buf = head + "\n", tail
        if len(buf) > _FLUSH_AT:
            out, buf = out + buf[:-_HOLD_BACK], buf[-_HOLD_BACK:]
        self._buf[name] = buf
        return [self._record_stream(name, out)] if out else []

    def flush(self) -> list[tuple[str, str]]:
        res = []
        for name in ("stdout", "stderr"):
            if self._buf[name]:
                res.append((name, self._record_stream(name, self._buf[name])))
                self._buf[name] = ""
        return res

    def _record_stream(self, name: str, raw: str) -> str:
        text = self.scrubber.scrub(raw)
        self._add({"type": "stream", "name": name, "text": text}, len(text.encode("utf-8", "replace")))
        return text

    # ---- rich output ----
    def display(self, data: dict[str, Any], metadata: dict[str, Any], is_result: bool) -> dict[str, Any]:
        if self.limit_exceeded:
            return {}
        clean: dict[str, Any] = {}
        for mime, val in data.items():
            if isinstance(val, str):
                clean[mime] = self.scrubber.scrub(val) if mime.startswith("text/") or mime.endswith(("json", "html")) else val
            else:
                clean[mime] = self.scrubber.scrub_obj(val)
        item = {"type": "execute_result" if is_result else "display_data", "data": clean}
        size = len(json.dumps(clean, default=str))
        self._add(item, size)
        return {k: (v if len(json.dumps(v, default=str)) <= _MAX_ITEM_BYTES else {"omitted_bytes": len(json.dumps(v, default=str))})
                for k, v in clean.items()}

    def note(self, text: str) -> None:
        self._add({"type": "note", "text": self.scrubber.scrub(text)}, len(text))

    # ---- storage ----
    def _add(self, item: dict[str, Any], size: int) -> None:
        if self.limit_exceeded:
            return
        self.total_bytes += size
        if self.total_bytes > self.max_total:
            self._trip_limit()
            return
        self._to_file(item) if self._file else self._all.append(item)
        if self._db_bytes + size <= self.max_db and not self.truncated:
            self._merge_or_append(item)
            self._db_bytes += size
        elif not self.truncated:
            self._truncate_with(item, size)
        # else: already truncated; the item lives in the artifact file only

    def _trip_limit(self) -> None:
        if self.limit_exceeded:
            return
        self.limit_exceeded = True
        self.truncated = True
        self._open_file()
        self._to_file({"type": "note", "text": f"output limit of {self.max_total} bytes exceeded; further output dropped"})
        self._db.append({"type": "note", "text": "output limit exceeded; further output dropped"})

    def _merge_or_append(self, item):
        if item["type"] == "stream" and self._db and self._db[-1].get("type") == "stream" \
                and self._db[-1]["name"] == item["name"]:
            self._db[-1]["text"] += item["text"]
        else:
            self._db.append(dict(item))

    def _truncate_with(self, item: dict[str, Any], size: int) -> None:
        room = max(self.max_db - self._db_bytes, 0)
        if item["type"] == "stream" and room > 0:
            self._merge_or_append({**item, "text": item["text"].encode("utf-8")[:room].decode("utf-8", "ignore")})
        self.truncated = True
        self._open_file()

    def _open_file(self) -> None:
        if self._file is None:
            self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.artifact_path, "w", encoding="utf-8")
            for it in self._all:
                self._to_file(it)
            self._all = []

    def _to_file(self, item: dict[str, Any]) -> None:
        if self._file:
            self._file.write(json.dumps(item, default=str) + "\n")

    def finish(self) -> tuple[list[dict[str, Any]], bool, str | None]:
        for name, _ in self.flush():
            pass
        if self.truncated:
            self._open_file()
            self._db.append({"type": "truncated", "note": "output truncated in the database; see full_output_path",
                             "total_bytes": self.total_bytes})
        if self._file:
            self._file.close()
        return self._db, self.truncated, str(self.artifact_path) if self.truncated else None
