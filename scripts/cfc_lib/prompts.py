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

from .git_ops import git_diff_stat, git_review_diff
from .state import collect_active_wiki

def format_list(items: list[str]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- `{x}`" for x in items)

def render_task(run: dict[str, Any]) -> str:
    g = run["guardrails"]
    v = run["verification"]
    return f"""# CfC Task: {run['title']}

## Goal

{run['title']}

## Repository

`{run['repo']}`

## Branch

`{run.get('branch')}`

## Allowed Paths

{format_list(g.get('allowed_paths', []))}

## Forbidden Paths

{format_list(g.get('forbidden_paths', []))}

## Forbidden Actions

{format_list(g.get('forbidden_actions', []))}

## Verification Commands

{format_list(v.get('commands', []))}

## Done Criteria

- Scope check passes.
- No forbidden files changed.
- Configured verification commands pass or the reason is explicitly recorded.
- Independent review has no BLOCKER findings for risky changes.
- CfC controller writes final artifacts under `.cfc/runs/<run-id>/`; workers must not create root-level `DONE.md`.
"""

def render_precheck(run: dict[str, Any]) -> str:
    pre = run["precheck"]
    return f"""# Precheck

- repo: `{run['repo']}`
- branch: `{pre.get('branch')}`
- dirty_before: `{pre.get('dirty')}`

## Git Status Before

```text
{pre.get('status_short') or '(clean)'}
```
"""

def render_minimality_gate() -> str:
    return """## Pre-Edit Minimality Gate

Before writing or generating code, answer these questions in your own working notes and let the answers constrain the diff:

1. Is this change truly required for the user's requested outcome, or is it optional polish?
2. Does the requested behavior already exist? Search for existing handlers, helpers, feature flags, config, routes, serializers, generated clients, tests, and UI flows before adding anything.
3. Can the outcome be achieved by reusing or wiring existing code instead of creating a new abstraction?
4. Can this be a one-line or tiny localized change? If yes, do that and stop there.
5. Can deletion, config, copy/text, a query/payload adjustment, or a narrow condition fix solve it without new code paths?
6. What is the smallest file set that must change? Do not touch files outside that set.
7. What would be overengineering here: new dependency, broad refactor, new framework pattern, generalized helper, large docs, or generated churn?
8. What cheap verification proves the minimal change works?

Decision rule:
- Prefer no code if existing behavior/config already satisfies the request.
- Prefer deletion or wiring over addition.
- Prefer the smallest local patch over reusable abstractions unless the current code already has that abstraction.
- If you cannot justify the need for new code in one sentence, do not add it.
- If the task is impossible or already done, report that instead of fabricating changes.
"""

def indent_block(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())

def build_prompt(run: dict[str, Any], root: Path, user_request: str, mode: str = "execute") -> str:
    g = run["guardrails"]
    v = run["verification"]
    wiki = collect_active_wiki(root)
    sections: list[str] = []
    sections.append(f"# CfC {mode.title()} Prompt")
    sections.append(f"Repository: {root}")
    sections.append(f"Current CfC run: {run['title']} ({run['id']})")
    sections.append("## Hard Rules")
    sections.append("- Plan before editing: first state files you will inspect/modify.")
    sections.append("- Complete the Pre-Edit Minimality Gate before writing or generating code.")
    sections.append("- Keep the diff minimal and scoped to the task.")
    sections.append("- Reuse existing behavior before adding code; prefer deletion/wiring/config over new abstractions.")
    sections.append("- Do not edit outside allowed paths unless you stop and explain why.")
    sections.append("- Do not create AGENTS.md, DONE.md, project memory files, or broad docs unless explicitly asked.")
    sections.append("- Do not write final reports into repository files. Report in the chat/pane only; CfC controller owns `.cfc/runs/<run-id>/DONE.md`.")
    sections.append("- Do not install dependencies, format the whole repo, stage, commit, or push.")
    sections.append("- Do not claim completion until verification commands ran or you explicitly explain why they cannot run.")
    sections.append("- If you find extra work, put it in Parking Lot; do not mix it into the current task.")
    sections.append(render_minimality_gate())
    sections.append("## Allowed Paths")
    sections.append(format_list(g.get("allowed_paths", [])))
    sections.append("## Forbidden Paths")
    sections.append(format_list(g.get("forbidden_paths", [])))
    sections.append("## Verification Commands")
    sections.append(format_list(v.get("commands", [])))
    if any(wiki.values()):
        sections.append("## Applicable CfC Wiki Knowledge")
        for key, items in wiki.items():
            if not items:
                continue
            sections.append(f"### {key.title()}")
            for title, body in items:
                sections.append(f"- {title}\n{indent_block(body, '  ')}")
    sections.append("## Required Final Report")
    sections.append("Report these items in the GJC chat/pane only; do not create or edit `DONE.md` or any report file in the repository.\n\n1. Files changed\n2. Why each file changed\n3. Verification commands and exact result\n4. Remaining risks/blockers\n5. Whether done criteria are met")
    sections.append("## User Request")
    sections.append(user_request)
    return "\n\n".join(sections).strip() + "\n"

def latest_artifact_excerpt(rd: Path, patterns: list[str], max_chars: int = 20000) -> str:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(p for p in rd.glob(pattern) if p.is_file())
    if not files:
        return "(none)"
    path = sorted(files, key=lambda p: p.stat().st_mtime)[-1]
    text = path.read_text(encoding="utf-8", errors="ignore")
    truncated = len(text) > max_chars
    suffix = "\n...<truncated>" if truncated else ""
    return f"Source: `{path.name}`\n\n```text\n{text[:max_chars]}{suffix}\n```"

def render_review_prompt(run: dict[str, Any], root: Path, rd: Path, iteration: int) -> str:
    check_text = (rd / "CHECK.md").read_text(encoding="utf-8", errors="ignore") if (rd / "CHECK.md").exists() else "(CHECK.md missing)"
    review_diff = git_review_diff(root)
    check = run.get("check", {}) or {}
    no_diff_fast_gate = check.get("verdict") == "PASS" and not check.get("changed_files")
    executor_excerpt = latest_artifact_excerpt(rd, [f"GJC_LOG.iteration-{iteration}.md", "GJC_LOG.*.md", f"EXECUTION.iteration-{iteration}.md"])
    fast_gate = ""
    if no_diff_fast_gate:
        fast_gate = """
Fast gate for no-diff runs:
- CHECK.md says PASS and Changed Files is empty.
- Do not inspect the repository, read broad docs, or run tests/verification commands.
- Review only the task contract, CHECK.md, DIFF/current-diff evidence, and executor report excerpt below.
- If those artifacts are internally consistent, return Verdict: PASS quickly.
- If the artifacts are incomplete or contradictory, return Verdict: REVIEW_BLOCKED with the artifact gap as the blocker.
"""
    return f"""# CfC Independent Review Prompt

Repository: {root}
Run: {run['title']} ({run['id']})
Iteration: {iteration}

You are the independent read-only reviewer in a fresh, clean context.
Do not edit, write, stage, commit, push, install dependencies, format files, run tests, or run verification commands.
Review only the task contract, current diff, and verification evidence below.
Ignore the executor's confidence; trust the diff and real evidence.
Do not perform a new repo-wide audit unless the current diff or CHECK evidence explicitly requires it.
{fast_gate}

Classify findings as:
- BLOCKER: requirement mismatch, runtime breakage, failing verification, API/payload contract break, data/security risk, forbidden scope change.
- MAJOR: important maintainability/test/edge-case issue that should be considered before merge.
- MINOR: small cleanup.

Required output format exactly:

Verdict: PASS or REVIEW_BLOCKED

## BLOCKERS
- none

## MAJOR
- none

## MINOR
- none

## Verification gaps
- none

## Suggested repair prompt
- none unless REVIEW_BLOCKED

## Task

{(rd / 'TASK.md').read_text(encoding='utf-8') if (rd / 'TASK.md').exists() else run['title']}

## Check Evidence

```md
{check_text[:20000]}
```

## Executor Report Excerpt

{executor_excerpt}

## Current Review Diff

{review_diff}
"""

def render_repair_prompt(run: dict[str, Any], root: Path, rd: Path, iteration: int, blockers: list[str]) -> str:
    blocker_text = "\n".join(f"- {b}" for b in blockers) or "- none"
    return f"""# CfC Repair Prompt

Repository: {root}
Run: {run['title']} ({run['id']})
Repair iteration: {iteration}

Fix only the BLOCKER findings below.
Before editing, repeat the Pre-Edit Minimality Gate specifically for each blocker: is it truly a blocker, does existing code already satisfy it, and what is the smallest repair?
Do not broaden scope, refactor unrelated code, install dependencies, stage, commit, or push.
Do not create or edit root-level `DONE.md` or any repository report file; report results in the chat/pane only.
Keep existing task contract and allowed/forbidden paths.
After the repair, run the configured verification commands and report exact results.

{render_minimality_gate()}

## BLOCKERS to fix

{blocker_text}

## Task Contract

{(rd / 'TASK.md').read_text(encoding='utf-8') if (rd / 'TASK.md').exists() else run['title']}

## Required final report

Report in the GJC chat/pane only; do not create or edit `DONE.md` or any report file in the repository.

1. Files changed
2. Which BLOCKER each change fixes
3. Verification commands and exact result
4. Remaining risks/blockers
"""

def render_blockers_md(review_path: Path | None, parsed: dict[str, Any]) -> str:
    blockers = parsed.get("blockers", [])
    return "# CfC Blockers\n\n" + f"Source review: `{review_path}`\n\n" + ("## BLOCKERS\n\n" + "\n".join(f"- {b}" for b in blockers) if blockers else "No BLOCKER findings.\n") + "\n"

def render_check(run: dict[str, Any], changed: list[str], outside: list[str], forbidden: list[str], verification: list[dict[str, Any]], failures: list[str], warnings: list[str], verdict: str) -> str:
    parts = [f"# CfC Check: {run['title']}", f"\n## Verdict\n\n**{verdict}**"]
    parts.append("## Changed Files\n\n```text\n" + ("\n".join(changed) or "(none)") + "\n```")
    parts.append("## Scope Check")
    parts.append("- outside allowed: " + (", ".join(outside) if outside else "none"))
    parts.append("- forbidden changed: " + (", ".join(forbidden) if forbidden else "none"))
    parts.append("## Diff Stat\n\n```text\n" + (git_diff_stat(Path(run['repo'])) or "(no diff)") + "\n```")
    parts.append("## Verification")
    if verification:
        for r in verification:
            parts.append(f"### `{r['command']}`\n\nexit: `{r['exit_code']}`\n\nstdout:\n```text\n{r['stdout']}\n```\n\nstderr:\n```text\n{r['stderr']}\n```")
    else:
        parts.append("(none)")
    if failures:
        parts.append("## Failures\n" + "\n".join(f"- {x}" for x in failures))
    if warnings:
        parts.append("## Warnings\n" + "\n".join(f"- {x}" for x in warnings))
    return "\n\n".join(parts) + "\n"
