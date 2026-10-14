"""KnowledgeIndex: BM25 over chunks of markdown/text files; the RAG seam (embeddings are a later swap)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from rank_bm25 import BM25Okapi

_TOKEN = re.compile(r"\w+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def chunk(text: str, chunk_chars: int) -> list[str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    cur = ""
    for p in paras:
        if cur and len(cur) + len(p) + 2 > chunk_chars:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


@dataclass(frozen=True)
class Hit:
    path: str
    score: float
    text: str


class KnowledgeIndex:
    def __init__(self, chunks: list[tuple[str, str]]) -> None:
        self._chunks = [(p, t) for p, t in chunks if tokenize(t)]  # BM25 divides by the mean length
        self._bm25 = BM25Okapi([tokenize(t) for _, t in self._chunks]) if self._chunks else None

    @classmethod
    def build(cls, directory: Path, *, chunk_chars: int = 1200) -> KnowledgeIndex:
        chunks: list[tuple[str, str]] = []
        for p in sorted(directory.rglob("*")):
            if p.suffix not in (".md", ".txt") or not p.is_file():
                continue
            rel = str(p.relative_to(directory))
            chunks += [(rel, c) for c in chunk(p.read_text(errors="replace"), chunk_chars)]
        return cls(chunks)

    @property
    def size(self) -> int:
        return len(self._chunks)

    def search(self, query: str, k: int = 3) -> list[Hit]:
        if self._bm25 is None:
            return []
        q = tokenize(query)
        if not q:
            return []
        scores = self._bm25.get_scores(q)
        qset = set(q)
        # BM25 scores can be ≤ 0 on tiny corpora (one document: negative idf), so overlap decides membership
        overlapping = [i for i in range(len(self._chunks)) if qset & set(tokenize(self._chunks[i][1]))]
        ranked = sorted(overlapping, key=lambda i: (-float(scores[i]), i))
        return [Hit(self._chunks[i][0], float(scores[i]), self._chunks[i][1]) for i in ranked[: max(1, k)]]
