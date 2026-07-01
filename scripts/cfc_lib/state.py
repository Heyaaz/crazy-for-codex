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
from .paths import current_file, ensure_cfc, global_wiki_dir, runs_dir, wiki_dir

def task_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*", text.lower()):
        parts = [raw, *raw.replace("_", "-").split("-")]
        tokens.update(p for p in parts if len(p) >= 3)
    return tokens

def parse_wiki_tags(text: str) -> set[str]:
    tags: set[str] = set()
    for m in re.finditer(r"(?im)^\s*tags\s*:\s*\[(.*?)\]\s*$", text):
        for tag in m.group(1).split(","):
            clean = tag.strip().strip("'\"").lower()
            if clean:
                tags.add(clean)
    in_tags = False
    for line in text.splitlines():
        if re.match(r"(?i)^\s*tags\s*:\s*$", line):
            in_tags = True
            continue
        if in_tags:
            item = re.match(r"^\s*-\s*(.+?)\s*$", line)
            if item:
                tags.add(item.group(1).strip().strip("'\"").lower())
                continue
            if line.strip() and not line.startswith((" ", "\t")):
                break
    return tags

def wiki_relevance_score(path: Path, text: str, title: str, task: set[str]) -> int:
    tags = parse_wiki_tags(text)
    if not task:
        return 0
    score = 0
    score += 5 * len(tags & task)
    score += 2 * len(task_tokens(title) & task)
    score += len(task_tokens(path.stem) & task)
    if "severity: high" in text.lower():
        score += 1
    return score

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

def wiki_override_keys(section: str, title: str, path: Path, source_id: str) -> set[tuple[str, str]]:
    return {
        (section, title.strip().lower()),
        (section, path.stem.strip().lower()),
        (section, source_id),
    }


def collect_wiki_entries(base: Path, scope: str, section: str, task: set[str]) -> list[tuple[int, str, str, str, dict[str, Any]]]:
    d = base / section
    if not d.exists():
        return []
    entries: list[tuple[int, str, str, str, dict[str, Any]]] = []
    for p in sorted(d.glob("*.md")):
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
        snippet = "\n".join(body)[:900]
        tags = sorted(parse_wiki_tags(text))
        score = wiki_relevance_score(p, text, title, task)
        reason_bits = []
        overlap_tags = sorted(set(tags) & task)
        if overlap_tags:
            reason_bits.append("tags: " + ", ".join(overlap_tags))
        if task_tokens(title) & task:
            reason_bits.append("title match")
        if task_tokens(p.stem) & task:
            reason_bits.append("filename match")
        if not reason_bits:
            reason_bits.append("fallback: no token overlap, kept as general memory")
        source_id = hashlib.sha256(f"{section}\n{title}\n{snippet}".encode("utf-8")).hexdigest()[:16]
        try:
            rel_path = str(p.relative_to(base))
        except ValueError:
            rel_path = p.name
        provenance = {
            "scope": scope,
            "section": section,
            "path": rel_path,
            "tags": tags,
            "score": score,
            "reason": "; ".join(reason_bits),
            "source_id": source_id,
            "override_keys": sorted("|".join(key) for key in wiki_override_keys(section, title, p, source_id)),
        }
        entries.append((score, p.name, title, snippet, provenance))
    if any(score > 0 for score, _, _, _, _ in entries):
        entries = [entry for entry in entries if entry[0] > 0]
    entries.sort(key=lambda entry: (-entry[0], entry[1]))
    return entries


def collect_active_wiki(root: Path, task_text: str = "", max_guardrails: int = 5, max_failures: int = 3, max_runbooks: int = 2) -> dict[str, list[tuple[str, str, dict[str, Any]]]]:
    """Collect repo-local and global active wiki items with provenance.

    Repo-local entries from ``<repo>/.cfc/wiki`` are preferred. Global entries
    from ``~/.cfc/wiki`` are supplementary and are skipped when a repo entry has
    the same title, slug, or source id. Prompt budgeting later keeps global
    memory smaller than repo memory.
    """
    out: dict[str, list[tuple[str, str, dict[str, Any]]]] = {"guardrails": [], "failures": [], "runbooks": []}
    repo_base = wiki_dir(root)
    global_base = global_wiki_dir()
    task = task_tokens(task_text)
    specs = [("guardrails", max_guardrails), ("failures", max_failures), ("runbooks", max_runbooks)]
    for section, limit in specs:
        repo_entries = collect_wiki_entries(repo_base, "repo", section, task)
        repo_keys: set[tuple[str, str]] = set()
        for _, _, title, _, provenance in repo_entries:
            repo_keys.update(tuple(item.split("|", 1)) for item in provenance.get("override_keys", []))

        global_entries: list[tuple[int, str, str, str, dict[str, Any]]] = []
        try:
            same_wiki = global_base.resolve() == repo_base.resolve()
        except OSError:
            same_wiki = False
        if not same_wiki:
            for entry in collect_wiki_entries(global_base, "global", section, task):
                provenance = entry[4]
                keys = {tuple(item.split("|", 1)) for item in provenance.get("override_keys", [])}
                if keys & repo_keys:
                    continue
                global_entries.append(entry)

        combined = repo_entries + global_entries
        out[section].extend((title, snippet, prov) for _, _, title, snippet, prov in combined[:limit])
    return out
