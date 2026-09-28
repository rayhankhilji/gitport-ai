"""Engineering-rule indexing and retrieval.

Step 1 of the gitport pipeline: index the repo's internal docs (design rules,
runbooks, post-mortems) with ``cohere.embed``, then for each incoming diff use
``cohere.rerank`` to pull the top-K most relevant rules into the prompt.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .config import Settings
from .models import RetrievedRule
from .store import VectorStore

_DOC_SUFFIXES = {".md", ".mdx", ".txt", ".rst"}
_HEADING_RE = re.compile(r"^#{1,4}\s+(.+)$")


class IndexError_(RuntimeError):
    pass


def _float_embeddings(res) -> list[list[float]]:
    """SDK exposes the float embedding field as ``float_`` on newer versions."""
    embs = getattr(res.embeddings, "float_", None) or getattr(res.embeddings, "float", None)
    if embs is None:
        raise IndexError_("cohere.embed returned no float embeddings")
    return list(embs)


def chunk_document(text: str, source: str, chunk_size: int, overlap: int) -> list[tuple[str, str]]:
    """Split a doc into (heading, content) chunks.

    Heading-aware: markdown sections become natural boundaries, then each
    section is windowed to ``chunk_size`` with ``overlap`` carry-over so a rule
    never gets split mid-sentence without context.
    """
    sections: list[tuple[str, str]] = []
    heading, buf = "", []
    for line in text.splitlines():
        if m := _HEADING_RE.match(line):
            if buf:
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if buf:
        sections.append((heading, "\n".join(buf).strip()))

    chunks: list[tuple[str, str]] = []
    for head, body in sections:
        if not body:
            continue
        if len(body) <= chunk_size:
            chunks.append((head, body))
            continue
        start = 0
        while start < len(body):
            chunks.append((head, body[start : start + chunk_size]))
            start += chunk_size - overlap
    return chunks


def _iter_docs(rules_dir: Path) -> list[Path]:
    return sorted(p for p in rules_dir.rglob("*") if p.suffix.lower() in _DOC_SUFFIXES)


def build_index(client, cfg: Settings, rules_dir: str | Path | None = None,
                index_path: str | Path | None = None) -> int:
    """Embed every doc chunk under ``rules_dir`` into the local vector store.

    Returns the number of chunks indexed.
    """
    docs_dir = Path(rules_dir or cfg.rules_dir)
    if not docs_dir.is_dir():
        raise IndexError_(f"rules directory not found: {docs_dir}")

    chunks: list[tuple[str, str, str]] = []  # (source, heading, content)
    for path in _iter_docs(docs_dir):
        rel = str(path.relative_to(docs_dir))
        for heading, content in chunk_document(
            path.read_text(encoding="utf-8", errors="replace"),
            rel, cfg.chunk_size, cfg.chunk_overlap,
        ):
            chunks.append((rel, heading, content))

    if not chunks:
        raise IndexError_(f"no documents found under {docs_dir}")

    texts = [c[2] for c in chunks]
    embeddings: list[list[float]] = []
    batch = 96  # cohere embed accepts up to 96 texts per call
    for i in range(0, len(texts), batch):
        res = client.embed(
            model=cfg.embed_model,
            texts=texts[i : i + batch],
            input_type="search_document",
            embedding_types=["float"],
        )
        embeddings.extend(_float_embeddings(res))

    store_path = index_path or cfg.index_path
    with VectorStore(store_path) as store:
        store.reset(cfg.embed_model)
        return store.add_many(
            (src, head, content, emb) for (src, head, content), emb in zip(chunks, embeddings)
        )


def retrieve_rules(client, cfg: Settings, query: str,
                   index_path: str | Path | None = None) -> list[RetrievedRule]:
    """Return the top-K rules relevant to this diff.

    Two-stage retrieval: cheap cosine over stored embeddings produces
    candidates, then ``cohere.rerank`` orders them by true relevance.
    """
    store_path = index_path or cfg.index_path
    if not Path(store_path).exists():
        return []

    with VectorStore(store_path) as store:
        if store.count() == 0:
            return []
        res = client.embed(
            model=cfg.embed_model,
            texts=[query[:8000]],
            input_type="search_query",
            embedding_types=["float"],
        )
        qvec = _float_embeddings(res)[0]
        candidates = store.top_k(qvec, cfg.embed_candidate_k)

    if not candidates:
        return []

    rr = client.rerank(
        model=cfg.rerank_model,
        query=query[:4000],
        documents=[c["content"] for c in candidates],
        top_n=min(cfg.top_k_rules, len(candidates)),
    )
    rules = []
    for r in rr.results:
        c = candidates[r.index]
        rules.append(
            RetrievedRule(
                source=c["source"],
                heading=c["heading"],
                excerpt=c["content"][:800],
                score=getattr(r, "relevance_score", 0.0),
            )
        )
    return rules


def rules_fingerprint(rules_dir: str | Path) -> str:
    """Stable fingerprint of the rules corpus, for cache headers/debugging."""
    h = hashlib.sha256()
    docs_dir = Path(rules_dir)
    if docs_dir.is_dir():
        for p in _iter_docs(docs_dir):
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()[:12]
