from __future__ import annotations

import re

from pydantic import BaseModel


class Chunk(BaseModel):
    index: int
    page_start: int  # 1-based
    page_end: int
    text: str


def chunk_pages(pages: list[str], max_chars: int = 1500, overlap: int = 200) -> list[Chunk]:
    """Split on whitespace boundaries into ~max_chars windows with overlap, remembering which pages each spans."""
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")
    # one stream of words, each tagged with its page
    words: list[tuple[str, int]] = [(w, i + 1) for i, p in enumerate(pages) for w in re.findall(r"\S+", p)]
    chunks: list[Chunk] = []
    start = 0
    while start < len(words):
        end, size = start, 0
        while end < len(words) and size + len(words[end][0]) + 1 <= max_chars:
            size += len(words[end][0]) + 1
            end += 1
        end = max(end, start + 1)  # always progress, even on a single huge token
        window = words[start:end]
        chunks.append(Chunk(index=len(chunks), page_start=window[0][1], page_end=window[-1][1], text=" ".join(w for w, _ in window)))
        if end >= len(words):
            break
        back, kept = end, 0
        while back > start + 1 and kept < overlap:
            back -= 1
            kept += len(words[back][0]) + 1
        start = max(back, start + 1)
    return chunks
