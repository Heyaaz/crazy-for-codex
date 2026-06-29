#!/usr/bin/env python3
"""CfC Recursive Harness.

Local-first external harness for GJC/Codex-style coding agents.
Stdlib-only by design.
"""
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
from pathlib import Path
from typing import Any

VERSION = "0.1.0"
CFC_DIR = ".cfc"
DEFAULT_FORBIDDEN_PATHS = [
    "AGENTS.md",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
]
DEFAULT_FORBIDDEN_ACTIONS = [
    "commit",
    "push",
    "dependency_install",
    "format_entire_repo",
    "broad_refactor",
]
DEFAULT_IGNORED_STATUS_PATTERNS = [
    CFC_DIR,
    f"{CFC_DIR}/**",
    "**/__pycache__/**",
    "**/__pycache__/",
    "*.pyc",
    "**/*.pyc",
]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9가-힣._-]+", "-", text.strip()).strip("-").lower()
    return s[:80] or "task"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def root_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "root", ".") or ".").expanduser().resolve()


def cfc_path(root: Path) -> Path:
    return root / CFC_DIR


def current_file(root: Path) -> Path:
    return cfc_path(root) / "current.json"


def runs_dir(root: Path) -> Path:
    return cfc_path(root) / "runs"


def wiki_dir(root: Path) -> Path:
    return cfc_path(root) / "wiki"


def ensure_cfc(root: Path) -> None:
    if not cfc_path(root).exists():
        raise SystemExit(f"CfC is not initialized in {root}. Run: cfc init --root {shlex.quote(str(root))}")


_MISSING = object()


def read_json(path: Path, default: Any = _MISSING) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not _MISSING:
            return default
        raise


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def append_ledger(run_dir: Path, phase: str, status: str, **data: Any) -> None:
    event = {"ts": now_iso(), "phase": phase, "status": status, **data}
    path = run_dir / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_cmd(cmd: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def shell_cmd(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)


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


def load_config(root: Path) -> dict[str, Any]:
    path = cfc_path(root) / "config.json"
    return read_json(path, default={})


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


def match_any(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat == "*":
            return True
        if fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat.rstrip("/") + "/"):
            return True
    return False


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


def cmd_init(args: argparse.Namespace) -> None:
    root = root_path(args)
    cfc = cfc_path(root)
    (cfc / "runs").mkdir(parents=True, exist_ok=True)
    for sub in ["failures", "guardrails", "patterns", "runbooks", "checklists"]:
        (cfc / "wiki" / sub).mkdir(parents=True, exist_ok=True)
    config = {
        "version": 1,
        "runner": {"type": "tmux", "tmux_target": "gjc:0.0"},
        "loop": {"max_iterations": 3, "require_independent_review": True, "fail_on_blocker": True, "require_verification": False},
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
- DONE.md records evidence and next action.
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


def format_list(items: list[str]) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- `{x}`" for x in items)


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
    sections.append("- Keep the diff minimal and scoped to the task.")
    sections.append("- Do not edit outside allowed paths unless you stop and explain why.")
    sections.append("- Do not create AGENTS.md, project memory files, or broad docs unless explicitly asked.")
    sections.append("- Do not install dependencies, format the whole repo, stage, commit, or push.")
    sections.append("- Do not claim completion until verification commands ran or you explicitly explain why they cannot run.")
    sections.append("- If you find extra work, put it in Parking Lot; do not mix it into the current task.")
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
    sections.append("1. Files changed\n2. Why each file changed\n3. Verification commands and exact result\n4. Remaining risks/blockers\n5. Whether done criteria are met")
    sections.append("## User Request")
    sections.append(user_request)
    return "\n\n".join(sections).strip() + "\n"


def indent_block(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def tmux_send(target: str, text: str) -> None:
    # paste-buffer is safer than send-keys for multiline prompts.
    subprocess.run(["tmux", "set-buffer", text], check=True)
    subprocess.run(["tmux", "paste-buffer", "-t", target], check=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def cmd_gjc(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    prompt = build_prompt(run, root, args.request, mode="execute")
    p = rd / f"PROMPT.iteration-{args.iteration}.md"
    p.write_text(prompt, encoding="utf-8")
    append_ledger(rd, "prompt", "done", iteration=args.iteration, path=str(p), sha256=sha256_text(prompt))
    print(f"Wrote prompt: {p}")
    if args.send:
        target = args.tmux_target or run.get("runner", {}).get("target") or "gjc:0.0"
        tmux_send(target, prompt)
        append_ledger(rd, "gjc_send", "sent", target=target, prompt=str(p))
        print(f"Sent prompt to tmux target: {target}")


def cmd_capture(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    target = args.tmux_target or run.get("runner", {}).get("target") or "gjc:0.0"
    p = subprocess.run(["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{args.lines}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        append_ledger(rd, "capture", "fail", target=target, error=p.stderr.strip())
        raise SystemExit(p.stderr.strip())
    out = rd / f"GJC_LOG.{dt.datetime.now().strftime('%H%M%S')}.md"
    out.write_text("# GJC Captured Log\n\n```text\n" + p.stdout + "\n```\n", encoding="utf-8")
    append_ledger(rd, "capture", "done", target=target, path=str(out))
    print(f"Captured tmux log: {out}")


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


def cmd_review(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    diff_text = git_diff(root)
    prompt = f"""# CfC Independent Review Prompt

Repository: {root}
Run: {run['title']} ({run['id']})

You are the independent read-only reviewer. Do not edit, write, stage, commit, or push.
Review only the current diff and task contract.

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

## Current Diff

```diff
{diff_text[:50000]}
```
"""
    path = rd / "REVIEW_PROMPT.md"
    path.write_text(prompt, encoding="utf-8")
    append_ledger(rd, "review_prompt", "done", path=str(path), sha256=sha256_text(prompt))
    print(f"Wrote review prompt: {path}")
    if args.send:
        target = args.tmux_target or run.get("runner", {}).get("target") or "gjc:0.0"
        tmux_send(target, prompt)
        append_ledger(rd, "review_send", "sent", target=target, prompt=str(path))
        print(f"Sent review prompt to tmux target: {target}")


def cmd_park(args: argparse.Namespace) -> None:
    root = root_path(args)
    _, rd = active_run(root)
    path = rd / "PARKING_LOT.md"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"- [{now_iso()}] {args.note}\n")
    append_ledger(rd, "park", "done", note=args.note)
    print(f"Parked: {args.note}")


def cmd_learn(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    check = (rd / "CHECK.md").read_text(encoding="utf-8", errors="ignore") if (rd / "CHECK.md").exists() else ""
    review_files = [p for p in sorted(rd.glob("REVIEW*.md")) if p.name != "REVIEW_PROMPT.md"]
    review_text = "\n\n".join(p.read_text(encoding="utf-8", errors="ignore")[:8000] for p in review_files)
    candidates = derive_learn_candidates(run, check, review_text)
    learn_md = render_learn(run, candidates)
    (rd / "LEARN.md").write_text(learn_md, encoding="utf-8")
    append_ledger(rd, "learn", "suggested", candidate_count=len(candidates), path=str(rd / "LEARN.md"))
    if args.apply:
        apply_candidates(root, run, candidates)
        append_ledger(rd, "learn_apply", "done", candidate_count=len(candidates))
        print("Applied learn candidates to .cfc/wiki")
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
    review_lower = review.lower()
    actual_review_blocker = (
        "verdict: review_blocked" in review_lower
        or "review_blocked" in review_lower
        or "remaining blocker" in review_lower
        or bool(re.search(r"(?im)^#+\s*blockers?\s*$\n\s*-\s*(?!(?:none|없음|\(none\))\s*$).+", review))
        or bool(re.search(r"(?im)^blockers?\s*:\s*(?!(?:none|없음|\(none\))\s*$).+", review))
    )
    if actual_review_blocker:
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


def cmd_done(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    verdict = run.get("check", {}).get("verdict")
    if verdict == "FAIL" and not args.force:
        raise SystemExit("Current check verdict is FAIL. Use --force only if you intentionally accept this.")
    if not (rd / "CHECK.md").exists() and not args.force:
        raise SystemExit("CHECK.md missing. Run cfc check first or use --force.")
    baseline_changed = set(run.get("precheck", {}).get("changed_files", []))
    changed = [f for f in git_changed_files(root) if f not in baseline_changed]
    done = f"""# CfC Done: {run['title']}

## Verdict

{verdict or 'UNKNOWN'}

## Changed Files

```text
{chr(10).join(changed) or '(none)'}
```

## Evidence

- CHECK.md: {'present' if (rd / 'CHECK.md').exists() else 'missing'}
- DIFF.md: {'present' if (rd / 'DIFF.md').exists() else 'missing'}
- LEARN.md: {'present' if (rd / 'LEARN.md').exists() else 'missing'}

## Next Action

Review CHECK.md and commit manually if the result is acceptable.
"""
    (rd / "DONE.md").write_text(done, encoding="utf-8")
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cfc", description="CfC recursive GJC harness")
    p.add_argument("--version", action="version", version=f"CfC {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_root(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default=".", help="Target repository root")

    sp = sub.add_parser("init")
    add_root(sp)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("start")
    add_root(sp)
    sp.add_argument("title")
    sp.add_argument("--allow", action="append")
    sp.add_argument("--forbid", action="append")
    sp.add_argument("--verify", action="append")
    sp.add_argument("--tmux-target")
    sp.add_argument("--allow-dirty", action="store_true", help="Allow starting with pre-existing dirty files as baseline evidence")
    sp.add_argument("--replace", action="store_true", help="Supersede the active run pointer with a new run")
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("status")
    add_root(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("gjc")
    add_root(sp)
    sp.add_argument("request")
    sp.add_argument("--iteration", type=int, default=1)
    sp.add_argument("--send", action="store_true")
    sp.add_argument("--tmux-target")
    sp.set_defaults(func=cmd_gjc)

    sp = sub.add_parser("capture")
    add_root(sp)
    sp.add_argument("--tmux-target")
    sp.add_argument("--lines", type=int, default=5000)
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser("check")
    add_root(sp)
    sp.set_defaults(func=cmd_check)

    sp = sub.add_parser("diff")
    add_root(sp)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("review")
    add_root(sp)
    sp.add_argument("--send", action="store_true")
    sp.add_argument("--tmux-target")
    sp.set_defaults(func=cmd_review)

    sp = sub.add_parser("park")
    add_root(sp)
    sp.add_argument("note")
    sp.set_defaults(func=cmd_park)

    sp = sub.add_parser("learn")
    add_root(sp)
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(func=cmd_learn)

    sp = sub.add_parser("done")
    add_root(sp)
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_done)

    sp = sub.add_parser("events")
    add_root(sp)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_events)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except subprocess.CalledProcessError as e:
        print(f"command failed: {e}", file=sys.stderr)
        return e.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())
