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

from .common import read_json
from .paths import current_file, ensure_cfc, runs_dir, wiki_dir

def active_run(root: Path) -> tuple[dict[str, Any], Path]:
    ensure_cfc(root)
    cur = read_json(current_file(root), default=None)
    if not cur or not cur.get("run_id"):
        raise SystemExit("No active CfC run. Use: cfc start \"task\"")
    rid = cur["run_id"]
    rd = runs_dir(root) / rid
    run = read_json(rd / "RUN.json")
    return run, rd

def current_active_run_or_none(root: Path) -> tuple[dict[str, Any], Path] | None:
    ensure_cfc(root)
    cur = read_json(current_file(root), default=None)
    if not cur or not cur.get("run_id"):
        return None
    rd = runs_dir(root) / cur["run_id"]
    if not (rd / "RUN.json").exists():
        return None
    run = read_json(rd / "RUN.json")
    if run.get("status") == "active":
        return run, rd
    return None

def collect_active_wiki(root: Path, max_guardrails: int = 5, max_failures: int = 3, max_runbooks: int = 2) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {"guardrails": [], "failures": [], "runbooks": []}
    base = wiki_dir(root)
    specs = [("guardrails", max_guardrails), ("failures", max_failures), ("runbooks", max_runbooks)]
    for section, limit in specs:
        d = base / section
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md"))[:limit]:
            text = p.read_text(encoding="utf-8", errors="ignore")
            if "status: retired" in text or "status: stale" in text:
                continue
            # Keep compact: title + prompt/check snippets if present.
            title = p.stem.replace("-", " ")
            m = re.search(r"^title:\s*(.+)$", text, re.M)
            if m:
                title = m.group(1).strip().strip('"')
            body = []
            for heading in ["# Rule", "# Prompt Patch", "# Prevention", "# Steps", "# Summary"]:
                idx = text.find(heading)
                if idx >= 0:
                    snippet = text[idx: idx + 900]
                    body.append(snippet.strip())
                    break
            out[section].append((title, "\n".join(body)[:900]))
    return out
