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

from .common import append_ledger, now_iso
from .paths import root_path, wiki_dir
from .review_result import parse_review_result
from .state import active_run

def run_learn(root: Path, run: dict[str, Any], rd: Path, apply: bool = False, auto_apply_high: bool = False) -> tuple[str, list[dict[str, str]], list[dict[str, str]]]:
    check = (rd / "CHECK.md").read_text(encoding="utf-8", errors="ignore") if (rd / "CHECK.md").exists() else ""
    review_files = sorted(rd.glob("REVIEW.iteration-*.md"))
    review_text = "\n\n".join(p.read_text(encoding="utf-8", errors="ignore")[:8000] for p in review_files)
    candidates = derive_learn_candidates(run, check, review_text)
    learn_md = render_learn(run, candidates)
    (rd / "LEARN.md").write_text(learn_md, encoding="utf-8")
    append_ledger(rd, "learn", "suggested", candidate_count=len(candidates), path=str(rd / "LEARN.md"))
    applied: list[dict[str, str]] = []
    if apply:
        applied = candidates
    elif auto_apply_high:
        applied = [c for c in candidates if c.get("severity") == "high"]
    if applied:
        apply_candidates(root, run, applied)
        append_ledger(rd, "learn_apply", "done", candidate_count=len(applied), mode="all" if apply else "high")
    return learn_md, candidates, applied

def cmd_learn(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    learn_md, _, applied = run_learn(root, run, rd, apply=args.apply)
    if applied:
        print(f"Applied {len(applied)} learn candidate(s) to .cfc/wiki")
    print(learn_md)

def derive_learn_candidates(run: dict[str, Any], check: str, review: str) -> list[dict[str, str]]:
    text = (check + "\n" + review).lower()
    check_verdict = str(run.get("check", {}).get("verdict", "")).upper()
    has_failures_section = "## failures" in text
    has_warnings_section = "## warnings" in text
    out: list[dict[str, str]] = []
    if check_verdict == "FAIL" and "verification failed" in text:
        out.append({
            "type": "Failure",
            "title": "Completion without reliable verification",
            "slug": "completion-without-reliable-verification",
            "severity": "high",
            "summary": "The run did not produce reliable verification evidence before completion.",
            "prevention": "Require configured verification commands or an explicit environment-blocker note before done.",
            "prompt_patch": "Do not claim completion until configured verification commands have been run and their exact results are reported, or explain why they cannot run.",
        })
    if has_warnings_section and "no verification commands configured" in text:
        out.append({
            "type": "Guardrail",
            "title": "Configure verification for non-trivial runs",
            "slug": "configure-verification-for-non-trivial-runs",
            "severity": "medium",
            "summary": "The run had no configured verification command, so completion evidence was weak.",
            "prevention": "Add at least one cheap scoped verification command for implementation runs.",
            "prompt_patch": "If no verification command is configured, explicitly propose the cheapest relevant verification command before editing.",
        })
    if check_verdict == "FAIL" and ("outside allowed" in text or "forbidden files changed" in text):
        out.append({
            "type": "Guardrail",
            "title": "No surprise files outside task scope",
            "slug": "no-surprise-files-outside-task-scope",
            "severity": "high",
            "summary": "The run touched files outside the allowed scope or hit forbidden path checks.",
            "prevention": "Stop before editing any file outside allowed paths; ask for scope expansion instead.",
            "prompt_patch": "Do not edit outside allowed paths. If the task requires it, stop and explain the required scope expansion first.",
        })
    parsed_review = parse_review_result(review) if review.strip() else {"verdict": "PASS", "blockers": []}
    review_blocker_text = "\n".join(parsed_review.get("blockers", []))
    if parsed_review.get("verdict") == "REVIEW_BLOCKED" and (
        "did not produce a final verdict" in review_blocker_text.lower()
        or "review evidence is incomplete" in review_blocker_text.lower()
        or "reviewer did not complete" in review_blocker_text.lower()
    ):
        out.append({
            "type": "Failure",
            "title": "Wait for reviewer verdict before classifying",
            "slug": "wait-for-reviewer-verdict-before-classifying",
            "severity": "high",
            "summary": "The run reached check/review but blocked because the reviewer pane had not produced a strict final Verdict line yet.",
            "prevention": "When awaiting a tmux reviewer, keep polling/capturing until a strict `Verdict: PASS` or `Verdict: REVIEW_BLOCKED` line appears; do not synthesize REVIEW_BLOCKED from an incomplete capture.",
            "prompt_patch": "Do not mark a review complete until the reviewer output contains a strict final `Verdict: PASS` or `Verdict: REVIEW_BLOCKED` line.",
        })
    if parsed_review.get("verdict") == "REVIEW_BLOCKED" and parsed_review.get("blockers"):
        out.append({
            "type": "Runbook",
            "title": "Blocker repair loop",
            "slug": "blocker-repair-loop",
            "severity": "medium",
            "summary": "Independent review found blockers; repairs should be scoped to blocker fixes only.",
            "prevention": "Feed only the blocker list into the next repair prompt and rerun check/review.",
            "prompt_patch": "Fix only the listed BLOCKER findings. Do not refactor or expand scope while repairing.",
        })
    # Deduplicate by slug, max 3.
    seen = set()
    uniq = []
    for c in out:
        if c["slug"] not in seen:
            seen.add(c["slug"])
            uniq.append(c)
    return uniq[:3]

def render_learn(run: dict[str, Any], candidates: list[dict[str, str]]) -> str:
    parts = [f"# Learn Candidates: {run['title']}"]
    if not candidates:
        parts.append("No strong learn candidates found. Keep this run as evidence only.")
    for i, c in enumerate(candidates, 1):
        parts.append(f"""## Candidate {i}: {c['type']}

Title: {c['title']}
Severity: {c['severity']}
Suggested slug: `{c['slug']}`

### Summary

{c['summary']}

### Prevention

{c['prevention']}

### Prompt Patch

> {c['prompt_patch']}
""")
    return "\n\n".join(parts) + "\n"

def apply_candidates(root: Path, run: dict[str, Any], candidates: list[dict[str, str]]) -> None:
    type_to_dir = {"Failure": "failures", "Guardrail": "guardrails", "Runbook": "runbooks"}
    for c in candidates:
        d = wiki_dir(root) / type_to_dir.get(c["type"], "patterns")
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{c['slug']}.md"
        if path.exists():
            continue
        path.write_text(f"""---
type: {c['type']}
title: {c['title']}
tags: [cfc, gjc]
status: active
severity: {c['severity']}
created_at: {now_iso()}
source_runs:
  - ../runs/{run['id']}
---

# Summary

{c['summary']}

# Prevention

{c['prevention']}

# Prompt Patch

{c['prompt_patch']}

# Evidence

Generated from CfC run `{run['id']}`. Review the run artifacts before treating this as a strong rule.
""", encoding="utf-8")
    log = wiki_dir(root) / "log.md"
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {now_iso()} Applied {len(candidates)} learn candidates from `{run['id']}`.\n")
