"""Embeddings behind a protocol (hosted vs local is an open decision; privacy mode needs local). The hashing embedder is a
deterministic, dependency-free stand-in for tests and offline use. A pgvector-backed index is deferred (decisions D-17)."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

from .chunk import Chunk


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    def __init__(self, dim: int = 2048) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for tok in re.findall(r"[a-z0-9]+", t.lower()):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                v[h % self.dim] += 1.0  # unsigned: a real match can never be cancelled by a collision
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class InMemoryVectorIndex:
    def __init__(self, embedder: Embedder) -> None:
        self._e = embedder
        self._chunks: list[Chunk] = []
        self._vecs: list[list[float]] = []

    def add(self, chunks: list[Chunk]) -> None:
        self._chunks.extend(chunks)
        self._vecs.extend(self._e.embed([c.text for c in chunks]))

    def search(self, query: str, k: int = 5) -> list[Chunk]:
        q = self._e.embed([query])[0]
        scored = sorted(((sum(a * b for a, b in zip(q, v, strict=True)), i) for i, v in enumerate(self._vecs)), key=lambda x: (-x[0], x[1]))
        return [self._chunks[i] for _, i in scored[:k]]
