"""Shared fixtures: a scripted Cohere client and a sample diff.

FakeCohereClient mimics the slice of ClientV2 that gitport uses —
chat/embed/rerank — so the whole pipeline is testable with no network and no
API key. Embeddings are deterministic character histograms (real cosine
signal), and rerank scores by word overlap, so retrieval tests assert real
behavior rather than echoes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def _fake_vector(text: str, dims: int = 64) -> list[float]:
    v = [0.0] * dims
    for ch in text.lower():
        v[ord(ch) % dims] += 1.0
    return v


def _tc(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"call_{name}",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _resp(text: str | None = None, tool_calls=None) -> SimpleNamespace:
    content = [SimpleNamespace(text=text)] if text is not None else None
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=tool_calls)
    )


class FakeCohereClient:
    """Scripted stand-in for cohere.ClientV2.

    tool_plan: list of rounds; each round is a list of (tool_name, args) the
    next chat() call should emit before finally answering with ``notes``.
    verdicts: list of dicts (or raw strings) returned for calls carrying
    response_format; later entries serve the repair retry.
    """

    def __init__(self, verdicts=None, tool_plan=None, notes="analysis notes"):
        self.chat_calls: list[dict] = []
        self.embed_calls: list[dict] = []
        self.rerank_calls: list[dict] = []
        self._verdicts = list(verdicts) if verdicts else [
            {"status": "PASSED", "breaking_changes_detected": False,
             "critical_flaws": [], "summary": "looks safe"},
        ]
        self._tool_plan = list(tool_plan or [])
        self._notes = notes

    # -- chat ------------------------------------------------------------
    def chat(self, **kw):
        self.chat_calls.append(kw)
        if "response_format" in kw:
            v = self._verdicts.pop(0) if len(self._verdicts) > 1 else self._verdicts[0]
            return _resp(text=v if isinstance(v, str) else json.dumps(v))
        if self._tool_plan:
            calls = [_tc(n, a) for n, a in self._tool_plan.pop(0)]
            return _resp(tool_calls=calls)
        return _resp(text=self._notes)

    # -- embed -----------------------------------------------------------
    def embed(self, model, texts, input_type=None, embedding_types=None, **kw):
        self.embed_calls.append({"model": model, "texts": list(texts),
                                 "input_type": input_type})
        return SimpleNamespace(embeddings=SimpleNamespace(
            float_=[_fake_vector(t) for t in texts]))

    # -- rerank ----------------------------------------------------------
    def rerank(self, model, query, documents, top_n=3, **kw):
        self.rerank_calls.append({"model": model, "query": query,
                                  "n_docs": len(documents), "top_n": top_n})
        qwords = set(query.lower().split())
        scored = []
        for i, doc in enumerate(documents):
            dwords = set(doc.lower().split())
            overlap = len(qwords & dwords) / (len(qwords) or 1)
            scored.append((overlap, i))
        scored.sort(key=lambda t: (-t[0], t[1]))
        return SimpleNamespace(results=[
            SimpleNamespace(index=i, relevance_score=s) for s, i in scored[:top_n]
        ])


SAMPLE_DIFF = """\
diff --git a/migrations/0012_add_email.sql b/migrations/0012_add_email.sql
new file mode 100644
index 0000000..1111111
--- /dev/null
+++ b/migrations/0012_add_email.sql
@@ -0,0 +1,3 @@
+ALTER TABLE users ADD COLUMN email varchar(255) NOT NULL;
+CREATE INDEX idx_users_email ON users(email);
+DELETE FROM sessions;
diff --git a/app/util.py b/app/util.py
index abc1234..def5678 100644
--- a/app/util.py
+++ b/app/util.py
@@ -1,2 +1,3 @@
 def helper():
-    return 1
+    return eval("1+1")
"""


@pytest.fixture
def fake_client():
    return FakeCohereClient()


@pytest.fixture
def sample_diff():
    return SAMPLE_DIFF


@pytest.fixture
def cfg(tmp_path):
    from gitport.config import Settings
    return Settings(
        index_path=str(tmp_path / "index.sqlite3"),
        rules_dir=str(tmp_path / "rules"),
        _env_file=None,
    )
