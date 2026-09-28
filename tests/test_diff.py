from gitport.diff import diff_digest, parse_unified_diff


def test_parse_counts_and_paths(sample_diff):
    files = parse_unified_diff(sample_diff)
    assert len(files) == 2

    sql, py = files
    assert sql.path == "migrations/0012_add_email.sql"
    assert sql.is_new is True
    assert len(sql.added) == 3
    assert [ln.lineno for ln in sql.added] == [1, 2, 3]

    assert py.path == "app/util.py"
    assert py.is_new is False
    assert py.deleted_count == 1
    assert len(py.added) == 1
    assert py.added[0].lineno == 2
    assert "eval" in py.added[0].text


def test_parse_empty_and_garbage():
    assert parse_unified_diff("") == []
    assert parse_unified_diff("not a diff\n") == []


def test_rename_tracking():
    diff = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "index abc..def 100644\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    files = parse_unified_diff(diff)
    assert files[0].path == "new.py"
    assert files[0].old_path == "old.py"


def test_digest_truncates(sample_diff):
    files = parse_unified_diff(sample_diff)
    digest = diff_digest(files, max_chars=200)
    assert len(digest) <= 240
    assert "<diff truncated>" in digest
