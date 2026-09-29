"""Deterministic detectors — secrets, language patterns, and config lints.

Each module is pure stdlib and returns the same shape as gitport.analysis:
{"ok", "file", "findings", ...} so the tool layer can treat them uniformly.
"""

from .confchecks import lint_config
from .langs import detect_language, scan_source_patterns
from .secrets import scan_text_for_secrets

__all__ = [
    "detect_language",
    "lint_config",
    "scan_source_patterns",
    "scan_text_for_secrets",
]
