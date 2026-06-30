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

from .common import append_ledger, now_iso, sha256_text
from .paths import root_path, wiki_dir
from .review_result import parse_review_result
from .state import active_run

def injected_wiki_fragments(rd: Path) -> list[str]:
    fragments: list[str] = []
    seen: set[str] = set()
    for prompt in sorted([*rd.glob("PROMPT*.md"), *rd.glob("REPAIR_PROMPT*.md")]):
        text = prompt.read_text(encoding="utf-8", errors="ignore")
        for block in re.findall(r"(?s)<!-- CFC:WIKI-SOURCE [^>]+ BEGIN -->(.*?)<!-- CFC:WIKI-SOURCE [^>]+ END -->", text):
            for line in block.splitlines():
                clean = line.strip().lstrip("-").strip()
                if clean.startswith("#"):
                    continue
                if len(clean) < 24:
                    continue
                key = clean.lower()
                if key not in seen:
                    seen.add(key)
                    fragments.append(clean)
    return fragments

def is_injected_fragment(text: str, fragments: list[str]) -> bool:
    normalized = re.sub(r"\s+", " ", text.strip()).lower()
    if len(normalized) < 24:
        return False
    for fragment in fragments:
        f = re.sub(r"\s+", " ", fragment.strip()).lower()
        if len(f) >= 24 and (normalized in f or f in normalized):
            return True
    return False

def remove_injected_wiki_text(text: str, fragments: list[str]) -> str:
    cleaned = text
    for fragment in sorted(fragments, key=len, reverse=True):
        cleaned = re.sub(re.escape(fragment), "", cleaned, flags=re.IGNORECASE)
    return cleaned

def read_source_artifacts(rd: Path) -> tuple[str, str, list[dict[str, str]]]:
    check = ""
    sources: list[dict[str, str]] = []
    check_path = rd / "CHECK.md"
    if check_path.exists():
        check = check_path.read_text(encoding="utf-8", errors="ignore")
        sources.append({"kind": "check", "path": check_path.name, "sha256": sha256_text(check)})
    review_files = sorted(rd.glob("REVIEW.iteration-*.md"))
    review_chunks: list[str] = []
    for p in review_files:
        text = p.read_text(encoding="utf-8", errors="ignore")
        sources.append({"kind": "review", "path": p.name, "sha256": sha256_text(text)})
        review_chunks.append(text[:8000])
    return check, "\n\n".join(review_chunks), sources

def run_learn(root: Path, run: dict[str, Any], rd: Path, apply: bool = False, auto_apply_high: bool = False) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    check, review_text, source_artifacts = read_source_artifacts(rd)
    candidates = derive_learn_candidates(run, check, review_text, injected_wiki_fragments(rd), source_artifacts)
    learn_md = render_learn(run, candidates)
    (rd / "LEARN.md").write_text(learn_md, encoding="utf-8")
    append_ledger(rd, "learn", "suggested", candidate_count=len(candidates), path=str(rd / "LEARN.md"))
    applied: list[dict[str, Any]] = []
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

def derive_learn_candidates(
    run: dict[str, Any],
    check: str,
    review: str,
    injected_fragments: list[str] | None = None,
    source_artifacts: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    injected_fragments = injected_fragments or []
    clean_review = remove_injected_wiki_text(review, injected_fragments)
    text = (check + "\n" + clean_review).lower()
    check_verdict = str(run.get("check", {}).get("verdict", "")).upper()
    has_failures_section = "## failures" in text
    has_warnings_section = "## warnings" in text
    out: list[dict[str, Any]] = []
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
    if review.strip() and injected_fragments:
        original_parsed = parse_review_result(review)
        remaining = [b for b in original_parsed.get("blockers", []) if not is_injected_fragment(b, injected_fragments)]
        if original_parsed.get("verdict") == "REVIEW_BLOCKED" and original_parsed.get("blockers") and not remaining:
            parsed_review = {"verdict": "PASS", "blockers": [], "major": [], "minor": []}
        else:
            parsed_review = parse_review_result(clean_review)
            parsed_review["blockers"] = [b for b in parsed_review.get("blockers", []) if not is_injected_fragment(b, injected_fragments)]
    else:
        parsed_review = parse_review_result(clean_review) if clean_review.strip() else {"verdict": "PASS", "blockers": []}
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
    evidence_hash = sha256_text(check + "\n" + review)
    for candidate in uniq:
        candidate["source_artifacts"] = list(source_artifacts or [])
        candidate["evidence_sha256"] = evidence_hash
    return uniq[:3]

def render_learn(run: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
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
        if c.get("source_artifacts"):
            parts.append("### Source Artifacts\n\n" + "\n".join(
                f"- `{a.get('path')}` ({a.get('kind')}): `{a.get('sha256')}`"
                for a in c.get("source_artifacts", [])
            ))
        if c.get("evidence_sha256"):
            parts.append(f"Evidence SHA256: `{c['evidence_sha256']}`")
    return "\n\n".join(parts) + "\n"

def render_source_artifacts_yaml(artifacts: list[dict[str, str]]) -> str:
    if not artifacts:
        return "source_artifacts: []"
    lines = ["source_artifacts:"]
    for artifact in artifacts:
        lines.append(f"  - kind: {artifact.get('kind', '')}")
        lines.append(f"    path: {artifact.get('path', '')}")
        lines.append(f"    sha256: {artifact.get('sha256', '')}")
    return "\n".join(lines)

def render_source_artifacts_md(artifacts: list[dict[str, str]]) -> str:
    if not artifacts:
        return "- none"
    return "\n".join(f"- `{a.get('path')}` ({a.get('kind')}): `{a.get('sha256')}`" for a in artifacts)

def apply_candidates(root: Path, run: dict[str, Any], candidates: list[dict[str, Any]]) -> None:
    type_to_dir = {"Failure": "failures", "Guardrail": "guardrails", "Runbook": "runbooks"}
    for c in candidates:
        d = wiki_dir(root) / type_to_dir.get(c["type"], "patterns")
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{c['slug']}.md"
        if path.exists():
            continue
        tags = sorted({"cfc", "gjc", *re.findall(r"[a-z0-9][a-z0-9_-]*", (c["title"] + " " + run["title"]).lower())})
        source_artifacts = c.get("source_artifacts") or []
        path.write_text(f"""---
type: {c['type']}
title: {c['title']}
tags: [{", ".join(tags[:12])}]
status: active
severity: {c['severity']}
created_at: {now_iso()}
source_runs:
  - ../runs/{run['id']}
evidence_sha256: {c.get('evidence_sha256', '')}
{render_source_artifacts_yaml(source_artifacts)}
---

# Summary

{c['summary']}

# Prevention

{c['prevention']}

# Prompt Patch

{c['prompt_patch']}

# Evidence

Generated from CfC run `{run['id']}`. Review the run artifacts before treating this as a strong rule.

Source artifacts:
{render_source_artifacts_md(source_artifacts)}

Evidence SHA256: `{c.get('evidence_sha256', '')}`
""", encoding="utf-8")
    log = wiki_dir(root) / "log.md"
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {now_iso()} Applied {len(candidates)} learn candidates from `{run['id']}`.\n")
