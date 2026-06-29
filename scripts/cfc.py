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

VERSION = "0.4.0"
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

## Current Review Diff

{git_review_diff(root)}
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
    review_files = sorted(rd.glob("REVIEW.iteration-*.md"))
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
    parsed_review = parse_review_result(review) if review.strip() else {"verdict": "PASS", "blockers": []}
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


def extract_review_result_name(iteration: int) -> str:
    return f"REVIEW.iteration-{iteration}.md"


def render_review_prompt(run: dict[str, Any], root: Path, rd: Path, iteration: int) -> str:
    check_text = (rd / "CHECK.md").read_text(encoding="utf-8", errors="ignore") if (rd / "CHECK.md").exists() else "(CHECK.md missing)"
    return f"""# CfC Independent Review Prompt

Repository: {root}
Run: {run['title']} ({run['id']})
Iteration: {iteration}

You are the independent read-only reviewer in a fresh, clean context.
Do not edit, write, stage, commit, push, install dependencies, or format files.
Review only the task contract, current diff, and verification evidence below.
Ignore the executor's confidence; trust the diff and real evidence.

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

## Current Review Diff

{git_review_diff(root)}
"""


def parse_review_result(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"verdict": "REVIEW_BLOCKED", "blockers": ["review produced no output"], "major": [], "minor": []}
    verdict_match = re.search(r"(?im)^\s*Verdict\s*:\s*([A-Z_]+)", text)
    verdict = verdict_match.group(1).upper() if verdict_match else ("REVIEW_BLOCKED" if "REVIEW_BLOCKED" in text.upper() else "PASS")

    def section_items(name: str) -> list[str]:
        m = re.search(rf"(?ims)^##\s*{re.escape(name)}\s*$\n(.*?)(?=^##\s+|\Z)", text)
        if not m:
            # Also accept `BLOCKERS:` style.
            m2 = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+)$", text)
            if not m2:
                return []
            raw = m2.group(1).strip()
            return [] if raw.lower() in {"none", "(none)", "없음", "n/a"} else [raw]
        items = []
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower() in {"- none", "none", "- (none)", "(none)", "- 없음", "없음"}:
                continue
            if stripped.startswith("-"):
                items.append(stripped.lstrip("- ").strip())
            elif stripped:
                items.append(stripped)
        return items

    blockers = section_items("BLOCKERS") or section_items("BLOCKER")
    major = section_items("MAJOR")
    minor = section_items("MINOR")
    if verdict == "REVIEW_BLOCKED" and not blockers:
        blockers = ["review returned REVIEW_BLOCKED without parsed BLOCKERS"]
    blocked = verdict == "REVIEW_BLOCKED" or bool(blockers)
    return {"verdict": "REVIEW_BLOCKED" if blocked else "PASS", "blockers": blockers, "major": major, "minor": minor}


def latest_review_result(rd: Path) -> tuple[Path | None, dict[str, Any]]:
    files = [p for p in sorted(rd.glob("REVIEW.iteration-*.md")) if p.is_file()]
    if not files:
        return None, {"verdict": "PASS", "blockers": [], "major": [], "minor": []}
    path = files[-1]
    return path, parse_review_result(path.read_text(encoding="utf-8", errors="ignore"))


def render_blockers_md(review_path: Path | None, parsed: dict[str, Any]) -> str:
    blockers = parsed.get("blockers", [])
    return "# CfC Blockers\n\n" + f"Source review: `{review_path}`\n\n" + ("## BLOCKERS\n\n" + "\n".join(f"- {b}" for b in blockers) if blockers else "No BLOCKER findings.\n") + "\n"


def render_repair_prompt(run: dict[str, Any], root: Path, rd: Path, iteration: int, blockers: list[str]) -> str:
    blocker_text = "\n".join(f"- {b}" for b in blockers) or "- none"
    return f"""# CfC Repair Prompt

Repository: {root}
Run: {run['title']} ({run['id']})
Repair iteration: {iteration}

Fix only the BLOCKER findings below.
Do not broaden scope, refactor unrelated code, install dependencies, stage, commit, or push.
Keep existing task contract and allowed/forbidden paths.
After the repair, run the configured verification commands and report exact results.

## BLOCKERS to fix

{blocker_text}

## Task Contract

{(rd / 'TASK.md').read_text(encoding='utf-8') if (rd / 'TASK.md').exists() else run['title']}

## Required final report

1. Files changed
2. Which BLOCKER each change fixes
3. Verification commands and exact result
4. Remaining risks/blockers
"""


def run_agent_command(command: str, prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), input=prompt, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, timeout=timeout)


def cmd_classify_review(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    path = Path(args.review_file).expanduser().resolve() if args.review_file else latest_review_result(rd)[0]
    if not path or not path.exists():
        raise SystemExit("No REVIEW.iteration-*.md found. Pass --review-file or run cfc review/cfc loop first.")
    parsed = parse_review_result(path.read_text(encoding="utf-8", errors="ignore"))
    (rd / "BLOCKERS.md").write_text(render_blockers_md(path, parsed), encoding="utf-8")
    run["review"] = {"verdict": parsed["verdict"], "blockers": parsed["blockers"], "review_file": str(path), "classified_at": now_iso()}
    write_json(rd / "RUN.json", run)
    append_ledger(rd, "review_classify", parsed["verdict"].lower(), blocker_count=len(parsed["blockers"]), review_file=str(path))
    print(json.dumps(parsed, indent=2, ensure_ascii=False))


def cmd_repair(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    _, parsed = latest_review_result(rd)
    blockers = parsed.get("blockers", []) or run.get("review", {}).get("blockers", [])
    if not blockers:
        raise SystemExit("No blockers found to repair.")
    prompt = render_repair_prompt(run, root, rd, args.iteration, blockers)
    path = rd / f"REPAIR_PROMPT.iteration-{args.iteration}.md"
    path.write_text(prompt, encoding="utf-8")
    append_ledger(rd, "repair_prompt", "done", iteration=args.iteration, blocker_count=len(blockers), path=str(path))
    print(f"Wrote repair prompt: {path}")
    if args.executor_command:
        res = run_agent_command(args.executor_command, prompt, root, args.timeout)
        out = rd / f"REPAIR_RESULT.iteration-{args.iteration}.md"
        out.write_text(f"# Repair Result\n\nCommand: `{args.executor_command}`\nExit: `{res.returncode}`\n\n## stdout\n```text\n{res.stdout}\n```\n\n## stderr\n```text\n{res.stderr}\n```\n", encoding="utf-8")
        append_ledger(rd, "repair_command", "pass" if res.returncode == 0 else "fail", iteration=args.iteration, exit_code=res.returncode, path=str(out))
        print(f"Wrote repair result: {out}")
        if res.returncode != 0:
            raise SystemExit(res.returncode)
    elif args.send:
        target = args.tmux_target or run.get("runner", {}).get("target") or "gjc:0.0"
        tmux_send(target, prompt)
        append_ledger(rd, "repair_send", "sent", iteration=args.iteration, target=target, prompt=str(path))
        print(f"Sent repair prompt to tmux target: {target}")


def cmd_loop(args: argparse.Namespace) -> None:
    root = root_path(args)
    if not args.executor_command and not args.send:
        raise SystemExit("cfc loop requires an executor adapter: pass --executor-command or use --send with --executor-target")
    if not args.reviewer_command and not args.send:
        raise SystemExit("cfc loop requires an independent reviewer: pass --reviewer-command or use --send with --reviewer-target")
    if not cfc_path(root).exists():
        cmd_init(argparse.Namespace(root=str(root)))
    start_args = argparse.Namespace(
        root=str(root), title=args.request, allow=args.allow, forbid=args.forbid, verify=args.verify,
        tmux_target=args.executor_target, allow_dirty=args.allow_dirty, replace=args.replace,
    )
    cmd_start(start_args)
    run, rd = active_run(root)
    final_parsed = {"verdict": "UNKNOWN", "blockers": []}
    for iteration in range(1, args.max_iterations + 1):
        append_ledger(rd, "loop_iteration", "start", iteration=iteration)
        if iteration == 1:
            prompt = build_prompt(run, root, args.request, mode="execute")
            prompt_path = rd / f"PROMPT.iteration-{iteration}.md"
        else:
            prompt_path = rd / f"REPAIR_PROMPT.iteration-{iteration - 1}.md"
            if not prompt_path.exists():
                prompt = build_prompt(run, root, f"Repair iteration {iteration}: fix only the blockers from BLOCKERS.md and preserve scope.", mode="repair")
                prompt_path.write_text(prompt, encoding="utf-8")
            else:
                prompt = prompt_path.read_text(encoding="utf-8")
        if not prompt_path.exists():
            prompt_path.write_text(prompt, encoding="utf-8")
        append_ledger(rd, "execute_prompt", "done", iteration=iteration, path=str(prompt_path))
        if args.executor_command:
            res = run_agent_command(args.executor_command, prompt, root, args.timeout)
            out = rd / f"EXECUTION.iteration-{iteration}.md"
            out.write_text(f"# Execution Result\n\nCommand: `{args.executor_command}`\nExit: `{res.returncode}`\n\n## stdout\n```text\n{res.stdout}\n```\n\n## stderr\n```text\n{res.stderr}\n```\n", encoding="utf-8")
            append_ledger(rd, "execute_command", "pass" if res.returncode == 0 else "fail", iteration=iteration, exit_code=res.returncode, path=str(out))
            if res.returncode != 0:
                run["status"] = "execute_failed"
                write_json(rd / "RUN.json", run)
                raise SystemExit(res.returncode)
        elif args.send:
            tmux_send(args.executor_target, prompt)
            append_ledger(rd, "execute_send", "sent", iteration=iteration, target=args.executor_target)
            if args.tmux_wait_seconds:
                subprocess.run(["sleep", str(args.tmux_wait_seconds)], check=False)
                cap = subprocess.run(["tmux", "capture-pane", "-t", args.executor_target, "-p", "-S", f"-{args.capture_lines}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                (rd / f"GJC_LOG.iteration-{iteration}.md").write_text("# GJC Captured Log\n\n```text\n" + cap.stdout + "\n```\n", encoding="utf-8")
        else:
            print(f"Wrote executor prompt for iteration {iteration}: {prompt_path}")
        cmd_diff(argparse.Namespace(root=str(root)))
        cmd_check(argparse.Namespace(root=str(root)))
        run, rd = active_run(root)
        if run.get("check", {}).get("verdict") == "FAIL" and args.review_on_check_fail is False:
            final_parsed = {"verdict": "REVIEW_BLOCKED", "blockers": run.get("check", {}).get("failures", [])}
        else:
            review_prompt = render_review_prompt(run, root, rd, iteration)
            review_prompt_path = rd / f"REVIEW_PROMPT.iteration-{iteration}.md"
            review_prompt_path.write_text(review_prompt, encoding="utf-8")
            append_ledger(rd, "review_prompt", "done", iteration=iteration, path=str(review_prompt_path))
            if args.reviewer_command:
                res = run_agent_command(args.reviewer_command, review_prompt, root, args.timeout)
                if res.returncode != 0:
                    review_text = (
                        "Verdict: REVIEW_BLOCKED\n\n## BLOCKERS\n"
                        f"- reviewer command failed with exit {res.returncode}; review evidence is invalid\n\n"
                        "## reviewer stdout\n```text\n" + res.stdout + "\n```\n\n"
                        "## reviewer stderr\n```text\n" + res.stderr + "\n```\n"
                    )
                else:
                    review_text = res.stdout + ("\n\n## reviewer stderr\n```text\n" + res.stderr + "\n```\n" if res.stderr else "")
                append_ledger(rd, "review_command", "pass" if res.returncode == 0 else "fail", iteration=iteration, exit_code=res.returncode)
            elif args.send:
                tmux_send(args.reviewer_target, review_prompt)
                append_ledger(rd, "review_send", "sent", iteration=iteration, target=args.reviewer_target)
                if args.tmux_wait_seconds:
                    subprocess.run(["sleep", str(args.tmux_wait_seconds)], check=False)
                cap = subprocess.run(["tmux", "capture-pane", "-t", args.reviewer_target, "-p", "-S", f"-{args.capture_lines}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                review_text = cap.stdout
            else:
                raise SystemExit("cfc loop requires an independent reviewer: pass --reviewer-command or use --send with --reviewer-target")
            review_path = rd / extract_review_result_name(iteration)
            review_path.write_text(review_text, encoding="utf-8")
            final_parsed = parse_review_result(review_text)
            (rd / "BLOCKERS.md").write_text(render_blockers_md(review_path, final_parsed), encoding="utf-8")
            run["review"] = {"verdict": final_parsed["verdict"], "blockers": final_parsed["blockers"], "review_file": str(review_path), "classified_at": now_iso()}
            write_json(rd / "RUN.json", run)
            append_ledger(rd, "review_classify", final_parsed["verdict"].lower(), iteration=iteration, blocker_count=len(final_parsed["blockers"]))
        blockers = final_parsed.get("blockers", [])
        if not blockers and run.get("check", {}).get("verdict") != "FAIL":
            append_ledger(rd, "loop", "pass", iteration=iteration)
            break
        if iteration >= args.max_iterations:
            run["status"] = "review_blocked"
            write_json(rd / "RUN.json", run)
            append_ledger(rd, "loop", "review_blocked", iteration=iteration, blocker_count=len(blockers))
            break
        repair_prompt = render_repair_prompt(run, root, rd, iteration, blockers or run.get("check", {}).get("failures", []))
        repair_path = rd / f"REPAIR_PROMPT.iteration-{iteration}.md"
        repair_path.write_text(repair_prompt, encoding="utf-8")
        append_ledger(rd, "repair_prompt", "done", iteration=iteration, path=str(repair_path), blocker_count=len(blockers))
    run, rd = active_run(root)
    cmd_learn(argparse.Namespace(root=str(root), apply=args.apply_learn))
    run, rd = active_run(root)
    if run.get("status") != "review_blocked" and run.get("check", {}).get("verdict") != "FAIL" and not final_parsed.get("blockers"):
        cmd_done(argparse.Namespace(root=str(root), force=False))
    else:
        print("CfC loop ended review_blocked/failed. Inspect BLOCKERS.md and run artifacts.")


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


def known_commands() -> set[str]:
    return {
        "init", "start", "status", "gjc", "capture", "check", "diff", "review",
        "classify-review", "repair", "loop", "park", "learn", "done", "events", "chat",
    }


def default_loop_namespace(request: str, root: str = ".", replace: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        request=request,
        allow=["*"],
        forbid=None,
        verify=["git diff --check"],
        max_iterations=3,
        executor_target=os.environ.get("CFC_EXECUTOR_TARGET", "gjc:0.0"),
        reviewer_target=os.environ.get("CFC_REVIEWER_TARGET", "cfc-review:0.0"),
        send=True,
        tmux_wait_seconds=int(os.environ.get("CFC_TMUX_WAIT_SECONDS", "120")),
        capture_lines=5000,
        executor_command=None,
        reviewer_command=None,
        timeout=600,
        allow_dirty=False,
        replace=replace,
        apply_learn=False,
        review_on_check_fail=True,
    )


def print_chat_help() -> None:
    print("""CfC chat mode

Type a task request and CfC will run the recursive loop against the current repo.

Examples:
  README 정리해줘
  src 안에서 로그인 버그 고쳐줘

Slash commands:
  /help      show this help
  /status    show active run status
  /events    show recent active run events
  /exit      quit

Defaults:
  root: current directory
  allow: *
  verify: git diff --check
  executor target: $CFC_EXECUTOR_TARGET or gjc:0.0
  reviewer target: $CFC_REVIEWER_TARGET or cfc-review:0.0
""")


def cmd_chat(args: argparse.Namespace) -> None:
    root = root_path(args)
    print("CfC chat mode. Type /help for commands, /exit to quit.")
    while True:
        try:
            text = input("cfc> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not text:
            continue
        if text in {"/exit", "/quit", "exit", "quit"}:
            break
        if text == "/help":
            print_chat_help()
            continue
        if text == "/status":
            try:
                cmd_status(argparse.Namespace(root=str(root)))
            except SystemExit as exc:
                print(exc, file=sys.stderr)
            continue
        if text == "/events":
            try:
                cmd_events(argparse.Namespace(root=str(root), limit=20))
            except SystemExit as exc:
                print(exc, file=sys.stderr)
            continue
        cmd_loop(default_loop_namespace(text, root=str(root), replace=args.replace))


def run_bare_request(argv: list[str]) -> int:
    request_parts: list[str] = []
    root = "."
    replace = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root" and i + 1 < len(argv):
            root = argv[i + 1]
            i += 2
            continue
        if arg == "--replace":
            replace = True
            i += 1
            continue
        request_parts.append(arg)
        i += 1
    request = " ".join(request_parts).strip()
    if not request:
        cmd_chat(argparse.Namespace(root=root, replace=replace))
    else:
        cmd_loop(default_loop_namespace(request, root=root, replace=replace))
    return 0


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

    sp = sub.add_parser("classify-review")
    add_root(sp)
    sp.add_argument("--review-file")
    sp.set_defaults(func=cmd_classify_review)

    sp = sub.add_parser("repair")
    add_root(sp)
    sp.add_argument("--iteration", type=int, default=1)
    sp.add_argument("--send", action="store_true")
    sp.add_argument("--tmux-target")
    sp.add_argument("--executor-command")
    sp.add_argument("--timeout", type=int, default=600)
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("loop")
    add_root(sp)
    sp.add_argument("request")
    sp.add_argument("--allow", action="append")
    sp.add_argument("--forbid", action="append")
    sp.add_argument("--verify", action="append")
    sp.add_argument("--max-iterations", type=int, default=3)
    sp.add_argument("--executor-target", default="gjc:0.0")
    sp.add_argument("--reviewer-target", default="gjc:0.1")
    sp.add_argument("--send", action="store_true")
    sp.add_argument("--tmux-wait-seconds", type=int, default=0)
    sp.add_argument("--capture-lines", type=int, default=5000)
    sp.add_argument("--executor-command")
    sp.add_argument("--reviewer-command")
    sp.add_argument("--timeout", type=int, default=600)
    sp.add_argument("--allow-dirty", action="store_true")
    sp.add_argument("--replace", action="store_true")
    sp.add_argument("--apply-learn", action="store_true")
    sp.add_argument("--review-on-check-fail", action="store_true", default=True)
    sp.set_defaults(func=cmd_loop)

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

    sp = sub.add_parser("chat")
    add_root(sp)
    sp.add_argument("--replace", action="store_true")
    sp.set_defaults(func=cmd_chat)

    sp = sub.add_parser("events")
    add_root(sp)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_events)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return run_bare_request([])
    if argv[0] not in known_commands() and not argv[0].startswith("-"):
        return run_bare_request(argv)
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



