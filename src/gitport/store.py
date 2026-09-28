"""SQLite-backed vector store for indexed engineering-rule chunks.

Deliberately dependency-free: at documentation scale (hundreds to a few
thousand chunks) pure-Python cosine over sqlite rows is fast enough and keeps
``gitport index`` working anywhere Python runs. Swap this module for a real
vector DB if a corpus outgrows it — the interface is three functions.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Iterable
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    heading TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    embedding TEXT NOT NULL
);
"""


class VectorStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def reset(self, embed_model: str) -> None:
        self._conn.execute("DELETE FROM chunks")
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('embed_model', ?)",
            (embed_model,),
        )
        self._conn.commit()

    def embed_model(self) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = 'embed_model'"
        ).fetchone()
        return row[0] if row else None

    def add_many(self, rows: Iterable[tuple[str, str, str, list[float]]]) -> int:
        self._conn.executemany(
            "INSERT INTO chunks (source, heading, content, embedding) VALUES (?, ?, ?, ?)",
            ((s, h, c, json.dumps(e)) for s, h, c, e in rows),
        )
        self._conn.commit()
        return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]

    def top_k(self, query_embedding: list[float], k: int) -> list[dict]:
        """Return the k nearest chunks by cosine similarity."""
        scored = []
        for source, heading, content, blob in self._conn.execute(
            "SELECT source, heading, content, embedding FROM chunks"
        ):
            emb = json.loads(blob)
            scored.append((_cosine(query_embedding, emb), source, heading, content))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [
            {"score": s, "source": src, "heading": h, "content": c}
            for s, src, h, c in scored[:k]
        ]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
