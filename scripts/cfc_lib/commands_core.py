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

from .common import append_ledger, env_bool, match_any, now_iso, sha256_text, shell_cmd, slugify, write_json
from .config import load_config
from .constants import DEFAULT_FORBIDDEN_ACTIONS, DEFAULT_FORBIDDEN_PATHS
from .git_ops import git_branch, git_changed_files, git_diff, git_diff_stat, git_review_diff, git_status_short, parse_status_files, require_git
from .learn import run_learn
from .paths import cfc_path, current_file, ensure_cfc, root_path, runs_dir, wiki_dir
from .prompts import render_check, render_precheck, render_task
from .state import active_run, current_active_run_or_none
from .tmux_ops import send_tmux_prompt

def cmd_init(args: argparse.Namespace) -> None:
    root = root_path(args)
    cfc = cfc_path(root)
    (cfc / "runs").mkdir(parents=True, exist_ok=True)
    for sub in ["failures", "guardrails", "patterns", "runbooks", "checklists"]:
        (cfc / "wiki" / sub).mkdir(parents=True, exist_ok=True)
    config = {
        "version": 1,
        "runner": {"type": "tmux", "tmux_target": "gjc:0.0", "reviewer_target": "gjc:0.1"},
        "loop": {"max_iterations": 3, "require_independent_review": True, "fail_on_blocker": True, "require_verification": False, "auto_learn": True},
        "learning": {"enabled": True, "mode": "suggest", "max_new_items_per_run": 3},
        "defaults": {
            "forbidden_paths": DEFAULT_FORBIDDEN_PATHS,
            "forbidden_actions": DEFAULT_FORBIDDEN_ACTIONS,
        },
    }
    cfg_path = cfc / "config.json"
    if not cfg_path.exists():
        write_json(cfg_path, config)
    index = wiki_dir(root) / "index.md"
    if not index.exists():
        index.write_text("# CfC Wiki\n\n- [Failures](failures/)\n- [Guardrails](guardrails/)\n- [Patterns](patterns/)\n- [Runbooks](runbooks/)\n- [Checklists](checklists/)\n", encoding="utf-8")
    log = wiki_dir(root) / "log.md"
    if not log.exists():
        log.write_text("# CfC Wiki Log\n\n", encoding="utf-8")
    print(f"Initialized CfC in {cfc}")

def cmd_start(args: argparse.Namespace) -> None:
    root = root_path(args)
    ensure_cfc(root)
    require_git(root)
    existing = current_active_run_or_none(root)
    if existing and not args.replace:
        run, rd = existing
        raise SystemExit(
            f"Active CfC run already exists: {run.get('id')} ({run.get('title')}) at {rd}. "
            "Finish it with cfc done, or pass --replace to intentionally supersede it."
        )
    title = args.title
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    rid = f"{ts}-{slugify(title)}"
    rd = runs_dir(root) / rid
    config = load_config(root)
    forbidden = list(config.get("defaults", {}).get("forbidden_paths", DEFAULT_FORBIDDEN_PATHS))
    forbidden.extend(args.forbid or [])
    status_before = git_status_short(root)
    baseline_files = parse_status_files(status_before)
    baseline_report_artifacts = [f for f in baseline_files if f == "DONE.md"]
    if baseline_report_artifacts:
        raise SystemExit(
            "Refusing to start while repository report artifacts exist outside .cfc. "
            "Move/delete these files first; CfC owns final artifacts under .cfc/runs/<run-id>/.\n\n"
            f"Report artifacts:\n{chr(10).join(baseline_report_artifacts)}"
        )
    if baseline_files and not args.allow_dirty:
        raise SystemExit(
            "Refusing to start on a dirty worktree because baseline subtraction can hide later edits. "
            "Commit/stash existing work or pass --allow-dirty to accept that pre-existing dirty files are baseline evidence.\n\n"
            f"Current status:\n{chr(10).join(baseline_files)}"
        )
    rd.mkdir(parents=True, exist_ok=False)
    run = {
        "version": 1,
        "id": rid,
        "title": title,
        "repo": str(root),
        "branch": git_branch(root),
        "status": "active",
        "created_at": now_iso(),
        "completed_at": None,
        "runner": {"type": "tmux", "target": args.tmux_target or config.get("runner", {}).get("tmux_target", "gjc:0.0")},
        "guardrails": {
            "allowed_paths": args.allow or [],
            "forbidden_paths": forbidden,
            "forbidden_actions": config.get("defaults", {}).get("forbidden_actions", DEFAULT_FORBIDDEN_ACTIONS),
        },
        "verification": {"commands": args.verify or []},
        "loop": {"max_iterations": int(getattr(args, "max_iterations", 0) or config.get("loop", {}).get("max_iterations") or os.environ.get("CFC_MAX_ITERATIONS", "3"))},
        "precheck": {"branch": git_branch(root), "status_short": status_before, "changed_files": baseline_files, "dirty": bool(baseline_files)},
        "check": {},
    }
    write_json(rd / "RUN.json", run)
    write_json(current_file(root), {"run_id": rid, "path": str(rd), "updated_at": now_iso()})
    (rd / "TASK.md").write_text(render_task(run), encoding="utf-8")
    (rd / "PRECHECK.md").write_text(render_precheck(run), encoding="utf-8")
    (rd / "PARKING_LOT.md").write_text("# Parking Lot\n\n", encoding="utf-8")
    append_ledger(rd, "preflight", "pass", branch=run["branch"], dirty=run["precheck"]["dirty"])
    print(f"Started CfC run: {rid}")
    print(f"Run dir: {rd}")

def cmd_status(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    changed = git_changed_files(root) if (root / ".git").exists() else []
    print(f"CfC Run: {run['title']}")
    print(f"id: {run['id']}")
    print(f"status: {run['status']}")
    print(f"repo: {run['repo']}")
    print(f"branch: {git_branch(root)}")
    print(f"run_dir: {rd}")
    print("changed_files:")
    for f in changed:
        print(f"  - {f}")
    ledger = rd / "ledger.jsonl"
    if ledger.exists():
        lines = ledger.read_text(encoding="utf-8").splitlines()[-8:]
        print("recent_events:")
        for line in lines:
            try:
                ev = json.loads(line)
                print(f"  - [{ev.get('status')}] {ev.get('phase')} {ev.get('ts')}")
            except Exception:
                print(f"  - {line[:120]}")

def cmd_diff(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    text = f"""# Diff Summary

## Changed Files

```text
{chr(10).join(git_changed_files(root)) or '(none)'}
```

## Diff Stat

```text
{git_diff_stat(root) or '(no diff)'}
```

## Full Diff

```diff
{git_diff(root)}
```
"""
    path = rd / "DIFF.md"
    path.write_text(text, encoding="utf-8")
    append_ledger(rd, "diff", "done", path=str(path))
    print(f"Wrote diff: {path}")

def cmd_check(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    g = run["guardrails"]
    current_changed = git_changed_files(root)
    baseline_changed = set(run.get("precheck", {}).get("changed_files", []))
    verification_results = []
    for cmd in run.get("verification", {}).get("commands", []):
        res = shell_cmd(cmd, root)
        verification_results.append({
            "command": cmd,
            "exit_code": res.returncode,
            "stdout": res.stdout[-8000:],
            "stderr": res.stderr[-8000:],
        })
    # Verification commands can create generated artifacts. Re-read status after
    # they run so scope checks see the final worktree state.
    current_changed = git_changed_files(root)
    changed = [f for f in current_changed if f not in baseline_changed]
    allowed = g.get("allowed_paths", [])
    forbidden = g.get("forbidden_paths", [])
    outside_allowed = [] if not allowed else [f for f in changed if not match_any(f, allowed)]
    forbidden_changed = [f for f in changed if match_any(f, forbidden)]
    failures = []
    warnings = []
    if outside_allowed:
        failures.append(f"changed files outside allowed paths: {', '.join(outside_allowed)}")
    if forbidden_changed:
        failures.append(f"forbidden files changed: {', '.join(forbidden_changed)}")
    # CfC owns final report artifacts under .cfc/runs/<run-id>/. A worker creating
    # a new root-level DONE.md is a policy violation, but nested/existing DONE.md
    # files are not globally forbidden. cmd_start refuses a dirty baseline root
    # DONE.md, so a post-baseline changed DONE.md here means the worker created it.
    if "DONE.md" in changed:
        failures.append("repository report artifact created outside .cfc: DONE.md")
    for r in verification_results:
        if r["exit_code"] != 0:
            failures.append(f"verification failed: {r['command']} exit {r['exit_code']}")
    if not verification_results and run.get("verification", {}).get("commands"):
        failures.append("verification commands configured but no result recorded")
    if not run.get("verification", {}).get("commands"):
        warnings.append("no verification commands configured")
    verdict = "FAIL" if failures else "WARN" if warnings else "PASS"
    report = render_check(run, changed, outside_allowed, forbidden_changed, verification_results, failures, warnings, verdict)
    (rd / "CHECK.md").write_text(report, encoding="utf-8")
    run["check"] = {"verdict": verdict, "changed_files": changed, "failures": failures, "warnings": warnings, "checked_at": now_iso()}
    write_json(rd / "RUN.json", run)
    append_ledger(rd, "check", verdict.lower(), changed_files=changed, failures=failures, warnings=warnings)
    print(report)

def cmd_review(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    prompt = f"""# CfC Independent Review Prompt

Repository: {root}
Run: {run['title']} ({run['id']})

You are the independent read-only reviewer. Do not edit, write, stage, commit, push, run tests, or run verification commands.
Review only the current diff and task contract.
If there is no current diff and CHECK evidence is already PASS, do not inspect the full repository; return PASS unless the provided artifacts contradict each other.

Classify findings as:
- BLOCKER: requirement mismatch, runtime breakage, failing verification, API/payload contract break, data/security risk, forbidden scope change.
- MAJOR: important maintainability/test/edge-case issue.
- MINOR: small cleanup.

Required output:
1. Verdict: PASS / REVIEW_BLOCKED
2. BLOCKER list
3. MAJOR list
4. MINOR list
5. Verification gaps
6. Suggested repair prompt if blocked

## Task

{(rd / 'TASK.md').read_text(encoding='utf-8') if (rd / 'TASK.md').exists() else run['title']}

## Current Review Diff

{git_review_diff(root)}
"""
    path = rd / "REVIEW_PROMPT.md"
    path.write_text(prompt, encoding="utf-8")
    append_ledger(rd, "review_prompt", "done", path=str(path), sha256=sha256_text(prompt))
    print(f"Wrote review prompt: {path}")
    if args.send:
        target = args.tmux_target or run.get("runner", {}).get("reviewer_target") or run.get("runner", {}).get("target") or "gjc:0.0"
        send_tmux_prompt(run, rd, "review_send", target, prompt, prompt_path=str(path))
        run["awaiting"] = {"phase": "reviewer", "target": target, "prompt": str(path), "since": now_iso()}
        write_json(rd / "RUN.json", run)
        print(f"Sent review prompt to tmux target: {target}")

def cmd_park(args: argparse.Namespace) -> None:
    root = root_path(args)
    _, rd = active_run(root)
    path = rd / "PARKING_LOT.md"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- [{now_iso()}] {args.note}\n")
    append_ledger(rd, "park", "done", note=args.note)
    print(f"Parked: {args.note}")

def cmd_done(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    verdict = run.get("check", {}).get("verdict")
    baseline_changed = set(run.get("precheck", {}).get("changed_files", []))
    changed = [f for f in git_changed_files(root) if f not in baseline_changed]
    review = run.get("review") or {}
    review_files = sorted(rd.glob("REVIEW.iteration-*.md"))
    if not args.force:
        if run.get("awaiting"):
            raise SystemExit(f"Run is still awaiting external agent completion: {run['awaiting']}. Capture/classify the result or use --force intentionally.")
        if verdict == "FAIL":
            raise SystemExit("Current check verdict is FAIL. Use --force only if you intentionally accept this.")
        if not (rd / "CHECK.md").exists():
            raise SystemExit("CHECK.md missing. Run cfc check first or use --force.")
        if changed and not review_files:
            raise SystemExit("Independent review result missing. Run/send review, capture it as REVIEW.iteration-N.md, and classify it before cfc done.")
        if changed and not review:
            raise SystemExit("RUN.json has no classified review result. Run cfc classify-review before cfc done.")
        if str(review.get("verdict", "")).upper() == "REVIEW_BLOCKED":
            raise SystemExit("Independent review is REVIEW_BLOCKED. Repair blockers or use --force intentionally.")
    if not getattr(args, "no_auto_learn", False):
        # High-severity learn candidates are never auto-applied under --force.
        # Force-finalizing a run does not silently mutate the wiki; the only way
        # to apply candidates under --force is the explicit --apply-learn flag
        # (which applies ALL candidates, not only high-severity ones).
        auto_apply_high = (
            env_bool("CFC_DONE_AUTO_APPLY_HIGH_LEARN", False)
            and not args.force
            and verdict == "PASS"
            and str(review.get("verdict", "PASS")).upper() != "REVIEW_BLOCKED"
        )
        learn_md, learn_candidates, applied_candidates = run_learn(
            root,
            run,
            rd,
            apply=getattr(args, "apply_learn", False),
            auto_apply_high=auto_apply_high,
        )
        if applied_candidates:
            mode_label = "learn" if getattr(args, "apply_learn", False) else "high-confidence learn"
            print(f"Applied {len(applied_candidates)} {mode_label} candidate(s) to .cfc/wiki")
        elif learn_candidates:
            print(f"Wrote LEARN.md with {len(learn_candidates)} candidate(s); none auto-applied")
        else:
            print("Wrote LEARN.md; no strong learn candidates")
    done = f"""# CfC Done: {run['title']}

## Verdict

{verdict or 'UNKNOWN'}

## Changed Files

```text
{chr(10).join(changed) or '(none)'}
```

## Evidence

- CHECK.md: {'present' if (rd / 'CHECK.md').exists() else 'missing'}
- REVIEW.iteration-*.md: {'present' if review_files else 'missing'}
- Classified review: {review.get('verdict', 'missing') if review else 'missing'}
- DIFF.md: {'present' if (rd / 'DIFF.md').exists() else 'missing'}
- LEARN.md: {'present' if (rd / 'LEARN.md').exists() else 'missing'}

## Next Action

Review CHECK.md and commit manually if the result is acceptable.
"""
    (rd / "DONE.md").write_text(done, encoding="utf-8")
    run.pop("awaiting", None)
    run["status"] = "done"
    run["completed_at"] = now_iso()
    write_json(rd / "RUN.json", run)
    write_json(current_file(root), {"run_id": None, "last_run_id": run["id"], "updated_at": now_iso()})
    append_ledger(rd, "done", "done", forced=args.force, verdict=verdict)
    print(done)

def cmd_events(args: argparse.Namespace) -> None:
    root = root_path(args)
    _, rd = active_run(root)
    path = rd / "ledger.jsonl"
    if not path.exists():
        print("No events yet")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines[-args.limit:]:
        print(line)
