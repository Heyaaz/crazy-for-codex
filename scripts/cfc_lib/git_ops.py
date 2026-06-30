from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .common import match_any, run_cmd
from .constants import CFC_DIR, DEFAULT_IGNORED_STATUS_PATTERNS, VERSION
from .paths import cfc_path, root_path

def default_status_patterns() -> list[str]:
    return DEFAULT_IGNORED_STATUS_PATTERNS + [
        "*.egg-info/**",
        ".pytest_cache/**",
        ".mypy_cache/**",
        ".ruff_cache/**",
    ]

def nearest_git_root(start: Path) -> Path:
    p = start.resolve()
    if p.is_file():
        p = p.parent
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            return start.resolve()
        p = p.parent

def is_git_repo(root: Path) -> bool:
    return git_output(root, "rev-parse", "--show-toplevel")[0] == 0

def discover_nested_git_roots(root: Path, max_depth: int = 3) -> list[Path]:
    root = root.resolve()
    if not root.exists() or not root.is_dir():
        return []
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    ignored = {".git", CFC_DIR, "node_modules", ".venv", "venv", "build", "dist", ".dart_tool", ".next"}
    while stack:
        cur, depth = stack.pop()
        if cur != root and (cur / ".git").exists():
            found.append(cur)
            continue
        if depth >= max_depth:
            continue
        try:
            children = sorted([c for c in cur.iterdir() if c.is_dir()], reverse=True)
        except OSError:
            continue
        for child in children:
            if child.name in ignored or child.name.startswith(".omx"):
                continue
            stack.append((child, depth + 1))
    return sorted(found)

def non_repo_payload(root: Path) -> dict[str, Any]:
    nested = discover_nested_git_roots(root)
    return {
        "version": VERSION,
        "repo": str(root),
        "is_git_repo": False,
        "branch": None,
        "dirty": False,
        "changed_files": [],
        "initialized": cfc_path(root).exists(),
        "active_run": None,
        "error": "not_a_git_repository",
        "message": f"Not a git repository: {root}",
        "nested_git_roots": [str(p) for p in nested],
        "hint": "Choose one nested_git_roots entry and retry with --root, or run separate per-repo CfC loops.",
    }

def resolve_plugin_root(args: argparse.Namespace) -> Path:
    requested = root_path(args)
    root = nearest_git_root(requested)
    if not is_git_repo(root):
        raise SystemExit(json.dumps(non_repo_payload(requested), indent=2, ensure_ascii=False))
    return root

def git_output(root: Path, *args: str) -> tuple[int, str, str]:
    p = run_cmd(["git", *args], root)
    return p.returncode, p.stdout, p.stderr

def require_git(root: Path) -> None:
    code, out, _ = git_output(root, "rev-parse", "--show-toplevel")
    if code != 0:
        raise SystemExit(f"Not a git repository: {root}")
    top = Path(out.strip()).resolve()
    if top != root:
        # Accept subdirs, but normalize mental model in output.
        pass

def git_branch(root: Path) -> str | None:
    code, out, _ = git_output(root, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0:
        return None
    return out.strip()

def git_status_short(root: Path) -> str:
    code, out, err = git_output(root, "status", "--short")
    if code != 0:
        return err.strip()
    return out.rstrip()

def ignored_status_path(path: str) -> bool:
    return match_any(path, DEFAULT_IGNORED_STATUS_PATTERNS)

def parse_status_files(status: str) -> list[str]:
    files: list[str] = []
    for line in status.splitlines():
        if not line.strip():
            continue
        # porcelain short: XY path or XY old -> new
        path = line[3:].strip()
        paths = [path]
        if " -> " in path:
            old, new = path.split(" -> ", 1)
            paths = [old.strip(), new.strip()]
        for item in paths:
            # CfC's own local run/wiki state and language cache files are evidence
            # or tool byproducts, not product-scope changes.
            if ignored_status_path(item):
                continue
            files.append(item)
    # Preserve order while deduping old/new rename paths.
    return list(dict.fromkeys(files))

def git_changed_files(root: Path) -> list[str]:
    return parse_status_files(git_status_short(root))

def git_diff_stat(root: Path) -> str:
    code, out, err = git_output(root, "diff", "--stat")
    return out.rstrip() if code == 0 else err.rstrip()

def git_diff(root: Path) -> str:
    code, out, err = git_output(root, "diff")
    return out if code == 0 else err

def git_review_diff(root: Path, max_file_chars: int = 20000) -> str:
    sections: list[str] = []
    code, status, err = git_output(root, "status", "--short")
    sections.append("## Git Status\n\n```text\n" + ((status if code == 0 else err).rstrip() or "(clean)") + "\n```")
    code, unstaged, err = git_output(root, "diff")
    sections.append("## Unstaged Diff\n\n```diff\n" + ((unstaged if code == 0 else err) or "(no unstaged diff)")[:50000] + "\n```")
    code, staged, err = git_output(root, "diff", "--cached")
    sections.append("## Staged Diff\n\n```diff\n" + ((staged if code == 0 else err) or "(no staged diff)")[:50000] + "\n```")
    # Plain git diff omits untracked files; include small text untracked files so
    # the clean reviewer can actually inspect new files.
    code, untracked, _ = git_output(root, "ls-files", "--others", "--exclude-standard")
    entries = []
    if code == 0:
        for rel in untracked.splitlines():
            rel = rel.strip()
            if not rel or ignored_status_path(rel):
                continue
            path = root / rel
            if not path.is_file():
                entries.append(f"### {rel}\n\n(non-file or directory)\n")
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                entries.append(f"### {rel}\n\n(unreadable: {exc})\n")
                continue
            if b"\0" in data:
                entries.append(f"### {rel}\n\n(binary file omitted, {len(data)} bytes)\n")
                continue
            text = data.decode("utf-8", errors="replace")
            truncated = len(text) > max_file_chars
            suffix = "\n...<truncated>" if truncated else ""
            entries.append(f"### {rel}\n\n```text\n{text[:max_file_chars]}{suffix}\n```\n")
    sections.append("## Untracked Files\n\n" + ("\n".join(entries) if entries else "(none)"))
    return "\n\n".join(sections)
