from __future__ import annotations

import os
import subprocess
from typing import Any

from pathlib import Path

from .constants import BUDGET_PRESETS, DEFAULT_BUDGET, FALSE_STRINGS, TRUE_STRINGS


def _normalize(name: str | None) -> str:
    if not name:
        return DEFAULT_BUDGET
    key = str(name).strip().lower()
    return key if key in BUDGET_PRESETS else DEFAULT_BUDGET


def _configured_budget_name(config: dict[str, Any] | None = None) -> str | None:
    if not config:
        return None
    raw = config.get("budget")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        value = raw.get("name") or raw.get("default")
        return str(value) if value else None
    return None


def budget_name(name: str | None = None, config: dict[str, Any] | None = None) -> str:
    return _normalize(name or os.environ.get("CFC_BUDGET") or _configured_budget_name(config))


def resolve_budget(name: str | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the merged budget preset for a budget name.

    Precedence: explicit `name` (CLI ``--budget``) > ``CFC_BUDGET`` env var >
    config ``budget`` > :data:`DEFAULT_BUDGET`. Per-key env overrides
    (``CFC_WIKI_CONTEXT_MAX_CHARS``, ``CFC_CAPTURE_LINES``,
    ``CFC_REVIEW_RISK_GATE``, review diff limits, and executor excerpt
    limits) still win over preset values at call sites.
    """
    return dict(BUDGET_PRESETS[budget_name(name, config)])


def budget_capture_lines(name: str | None = None, config: dict[str, Any] | None = None) -> int:
    explicit = os.environ.get("CFC_CAPTURE_LINES")
    if explicit is not None:
        try:
            return max(1, int(explicit))
        except ValueError:
            pass
    return int(resolve_budget(name, config)["capture_lines"])


def budget_review_risk_gate(name: str | None = None, config: dict[str, Any] | None = None) -> bool:
    explicit = os.environ.get("CFC_REVIEW_RISK_GATE")
    if explicit in TRUE_STRINGS:
        return True
    if explicit in FALSE_STRINGS:
        return False
    return bool(resolve_budget(name, config)["review_risk_gate"])


DOC_EXTENSIONS = {".md", ".mdx", ".rst", ".txt", ".adoc"}
DOC_BASENAMES = {"readme", "changelog", "license", "notice", "copying", "authors"}
TEST_PARTS = {"test", "tests", "__tests__", "spec", "specs"}
CONFIG_EXTENSIONS = {".json", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf"}
CONFIG_BASENAMES = {
    ".editorconfig",
    ".gitignore",
    ".prettierrc",
    ".prettierrc.json",
    ".eslintrc",
    ".eslintrc.json",
    "cfc.config.json",
}


def _parts(path: str) -> list[str]:
    return [part.lower() for part in Path(path).parts]


def is_docs_path(path: str) -> bool:
    p = Path(path)
    lower_parts = _parts(path)
    if "docs" in lower_parts or "documentation" in lower_parts:
        return True
    if p.suffix.lower() in DOC_EXTENSIONS:
        return True
    return p.name.lower().split(".", 1)[0] in DOC_BASENAMES


def is_test_path(path: str) -> bool:
    p = Path(path)
    lower_parts = set(_parts(path))
    name = p.name.lower()
    if lower_parts & TEST_PARTS:
        return True
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or ".test." in name
        or ".spec." in name
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.tsx")
    )


def is_config_path(path: str) -> bool:
    p = Path(path)
    name = p.name.lower()
    if name in CONFIG_BASENAMES:
        return True
    if p.suffix.lower() in CONFIG_EXTENSIONS:
        return True
    return name.startswith(".env.") or name.endswith(".env.example")


def _numstat_line_count(output: str) -> int:
    total = 0
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        for value in parts[:2]:
            if value == "-":
                total += 1000
            else:
                try:
                    total += int(value)
                except ValueError:
                    total += 1000
    return total


def changed_line_count(root: Path, changed_files: list[str]) -> int:
    total = 0
    for args in (["git", "diff", "--numstat"], ["git", "diff", "--cached", "--numstat"]):
        res = subprocess.run(args, cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if res.returncode == 0:
            total += _numstat_line_count(res.stdout)
    res = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    untracked = set(res.stdout.splitlines()) if res.returncode == 0 else set()
    for rel in changed_files:
        if rel not in untracked:
            continue
        path = root / rel
        if not path.exists() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            total += 1000
            continue
        if b"\0" in data:
            total += 1000
            continue
        total += len(data.decode("utf-8", errors="replace").splitlines())
    return total


def review_risk_gate_reason(root: Path, run: dict[str, Any]) -> str | None:
    check = run.get("check", {}) or {}
    if check.get("verdict") != "PASS":
        return None
    changed = list(check.get("changed_files") or [])
    if not changed:
        return "CHECK PASS with no product diff"
    if all(is_docs_path(path) for path in changed):
        return "CHECK PASS with docs-only changes"
    if all(is_test_path(path) for path in changed):
        return "CHECK PASS with test-only changes"
    line_count = changed_line_count(root, changed)
    if len(changed) <= 3 and line_count <= 80 and all(is_config_path(path) for path in changed):
        return f"CHECK PASS with tiny config-only changes ({len(changed)} files, {line_count} changed lines)"
    return None


def render_risk_gated_review(reason: str, iteration: int) -> str:
    return f"""Verdict: PASS

## BLOCKERS
- none

## MAJOR
- none

## MINOR
- none

## Verification gaps
- none

## Suggested repair prompt
- none

## CfC reviewer risk gate
- Reviewer adapter skipped for iteration {iteration}: {reason}.
"""
