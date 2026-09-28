from conftest import FakeCohereClient

from gitport.rules import build_index, chunk_document, retrieve_rules


def test_chunk_document_splits_on_headings():
    doc = "# Migrations\nnever add not null\n\n# Security\nno eval allowed\n"
    chunks = chunk_document(doc, "rules.md", chunk_size=500, overlap=50)
    headings = {h for h, _ in chunks}
    assert headings == {"Migrations", "Security"}
    assert all(body for _, body in chunks)


def test_chunk_document_windows_long_sections():
    doc = "# Big\n" + ("word " * 500)
    chunks = chunk_document(doc, "big.md", chunk_size=200, overlap=40)
    assert len(chunks) > 1
    assert all(len(body) <= 200 for _, body in chunks)


def test_index_and_retrieve(cfg, tmp_path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    (rules_dir / "migrations.md").write_text(
        "# Database migrations\n"
        "Always use CREATE INDEX CONCURRENTLY. Never add NOT NULL without "
        "a DEFAULT — it locks the table for the duration of the rewrite.\n"
    )
    (rules_dir / "style.md").write_text(
        "# Style guide\nWe prefer single quotes and short functions.\n"
    )

    client = FakeCohereClient()
    n = build_index(client, cfg, rules_dir, cfg.index_path)
    assert n >= 2
    assert client.embed_calls[0]["input_type"] == "search_document"

    rules = retrieve_rules(
        client, cfg,
        "migration adds NOT NULL column and creates index concurrently")
    assert len(rules) == cfg.top_k_rules or len(rules) == 2
    assert rules[0].source == "migrations.md"
    assert rules[0].score >= rules[-1].score
    assert client.rerank_calls


def test_retrieve_without_index_returns_empty(cfg):
    assert retrieve_rules(FakeCohereClient(), cfg, "q") == []
