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
import time
from pathlib import Path
from typing import Any

VERSION = "0.7.0"
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


def looks_like_cfc_dev_workspace(root: Path, dirty_files: list[str]) -> bool:
    # The old plugin workspace can contain the CfC prototype as untracked files.
    # For chat UX, treat that known dev workspace as baseline instead of blocking
    # every natural-language request.
    return root.resolve() == Path("/Users/byeonheejae/Documents/cfc").resolve() and any(
        f.startswith("plugins/cfc-recursive-harness/") or f.startswith("plugins/cfc-session-forensics/") or f == ".cfc" or f.startswith(".cfc/")
        for f in dirty_files
    )


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


def indent_block(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def tmux_send(target: str, text: str) -> None:
    # paste-buffer is safer than send-keys for multiline prompts.
    # Use load-buffer via stdin instead of `set-buffer <text>` so large CfC
    # prompts do not hit the OS argv/ARG_MAX limit ("command too long").
    subprocess.run(["tmux", "load-buffer", "-"], input=text, text=True, check=True)
    subprocess.run(["tmux", "paste-buffer", "-t", target], check=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def tmux_capture(target: str, lines: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", "capture-pane", "-t", target, "-p", "-S", f"-{lines}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def has_final_verdict(text: str) -> bool:
    return re.search(r"(?im)^\s*Verdict\s*:\s*(PASS|REVIEW_BLOCKED)\s*$", text) is not None


def wait_for_tmux_verdict(target: str, lines: int, poll_seconds: float = 5.0, timeout_seconds: int = 0) -> str:
    start = time.monotonic()
    while True:
        cap = tmux_capture(target, lines)
        if cap.returncode != 0:
            raise SystemExit(cap.stderr.strip())
        if has_final_verdict(cap.stdout):
            return cap.stdout
        if timeout_seconds and time.monotonic() - start >= timeout_seconds:
            raise TimeoutError(f"Timed out waiting for final Verdict from {target}")
        time.sleep(poll_seconds)


def short_run_token(run_id: str) -> str:
    head = run_id.split("-", 2)
    prefix = "-".join(head[:2]) if len(head) >= 2 else slugify(run_id)[:15]
    digest = sha256_text(run_id)[:8]
    return re.sub(r"[^A-Za-z0-9_-]+", "-", f"{prefix}-{digest}").strip("-")[:40]


def tmux_has_session(session: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode == 0


def ensure_gjc_tmux_session(session: str, root: Path, title: str) -> str:
    if not tmux_has_session(session):
        subprocess.run(["tmux", "new-session", "-d", "-s", session, "-c", str(root), "gjc"], check=True)
        subprocess.run(["tmux", "rename-window", "-t", f"{session}:0", title], check=False)
    return f"{session}:0.0"


def ensure_isolated_tmux_targets(root: Path, run: dict[str, Any], rd: Path) -> tuple[str, str]:
    token = short_run_token(run["id"])
    executor_session = f"cfc-{token}-exec"
    reviewer_session = f"cfc-{token}-review"
    executor_target = ensure_gjc_tmux_session(executor_session, root, "CFC executor")
    reviewer_target = ensure_gjc_tmux_session(reviewer_session, root, "CFC reviewer")
    run.setdefault("runner", {})["isolated_tmux"] = True
    run["runner"]["executor_session"] = executor_session
    run["runner"]["reviewer_session"] = reviewer_session
    run["runner"]["target"] = executor_target
    run["runner"]["reviewer_target"] = reviewer_target
    write_json(rd / "RUN.json", run)
    append_ledger(rd, "tmux_isolated", "ready", executor_target=executor_target, reviewer_target=reviewer_target)
    return executor_target, reviewer_target


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


def continue_after_executor_capture(root: Path, run: dict[str, Any], rd: Path, iteration: int) -> None:
    """After an async executor finishes, run checks and dispatch independent review."""
    run.pop("awaiting", None)
    write_json(rd / "RUN.json", run)
    cmd_diff(argparse.Namespace(root=str(root)))
    cmd_check(argparse.Namespace(root=str(root)))
    run, rd = active_run(root)
    review_prompt = render_review_prompt(run, root, rd, iteration)
    review_prompt_path = rd / f"REVIEW_PROMPT.iteration-{iteration}.md"
    review_prompt_path.write_text(review_prompt, encoding="utf-8")
    append_ledger(rd, "review_prompt", "done", iteration=iteration, path=str(review_prompt_path))
    reviewer_target = run.get("runner", {}).get("reviewer_target") or os.environ.get("CFC_REVIEWER_TARGET")
    if reviewer_target:
        tmux_send(reviewer_target, review_prompt)
        run["awaiting"] = {"phase": "reviewer", "iteration": iteration, "target": reviewer_target, "prompt": str(review_prompt_path), "since": now_iso()}
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "review_send", "sent", iteration=iteration, target=reviewer_target)
        append_ledger(rd, "async_loop", "waiting_for_reviewer", iteration=iteration, target=reviewer_target)
        print(f"CfC dispatched reviewer prompt to {reviewer_target} after executor capture.")
    else:
        append_ledger(rd, "async_loop", "review_prompt_ready", iteration=iteration, path=str(review_prompt_path))
        print(f"Wrote reviewer prompt: {review_prompt_path}")


def continue_after_review_classification(root: Path, run: dict[str, Any], rd: Path, iteration: int, parsed: dict[str, Any]) -> None:
    """After async review, either stop on pass or send BLOCKERS back to executor for repair."""
    blockers = parsed.get("blockers", [])
    if parsed.get("verdict") != "REVIEW_BLOCKED" and not blockers:
        append_ledger(rd, "async_loop", "review_pass", iteration=iteration)
        print("CfC review passed. Run `cfc done --root ...` to finalize.")
        return
    max_iterations = int(run.get("loop", {}).get("max_iterations") or os.environ.get("CFC_MAX_ITERATIONS", "3"))
    if iteration >= max_iterations:
        run["status"] = "review_blocked"
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "async_loop", "review_blocked", iteration=iteration, blocker_count=len(blockers), max_iterations=max_iterations)
        print("CfC review blocked and max iterations reached. Inspect BLOCKERS.md and LEARN.md.")
        return
    repair_prompt = render_repair_prompt(run, root, rd, iteration, blockers)
    repair_path = rd / f"REPAIR_PROMPT.iteration-{iteration}.md"
    repair_path.write_text(repair_prompt, encoding="utf-8")
    append_ledger(rd, "repair_prompt", "done", iteration=iteration, blocker_count=len(blockers), path=str(repair_path))
    executor_target = run.get("runner", {}).get("target") or os.environ.get("CFC_EXECUTOR_TARGET")
    if executor_target:
        tmux_send(executor_target, repair_prompt)
        next_iteration = iteration + 1
        run["awaiting"] = {"phase": "executor", "iteration": next_iteration, "target": executor_target, "prompt": str(repair_path), "since": now_iso(), "source_review_iteration": iteration}
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "repair_send", "sent", iteration=iteration, next_iteration=next_iteration, target=executor_target, prompt=str(repair_path))
        append_ledger(rd, "async_loop", "waiting_for_executor_repair", iteration=next_iteration, target=executor_target, blocker_count=len(blockers))
        print(f"CfC sent BLOCKERS to executor for repair: {executor_target}")
    else:
        append_ledger(rd, "async_loop", "repair_prompt_ready", iteration=iteration, path=str(repair_path), blocker_count=len(blockers))
        print(f"Wrote repair prompt: {repair_path}")


def cmd_capture(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    awaiting = run.get("awaiting") or {}
    is_awaiting_reviewer = awaiting.get("phase") == "reviewer"
    is_awaiting_executor = awaiting.get("phase") == "executor"
    target = args.tmux_target or awaiting.get("target") or run.get("runner", {}).get("target") or "gjc:0.0"
    wait_for_verdict = args.wait_verdict or (is_awaiting_reviewer and not args.no_wait_verdict)
    if wait_for_verdict:
        append_ledger(rd, "capture_wait", "start", target=target, timeout_seconds=args.timeout_seconds)
        try:
            text = wait_for_tmux_verdict(target, args.lines, poll_seconds=args.poll_seconds, timeout_seconds=args.timeout_seconds)
        except TimeoutError as e:
            append_ledger(rd, "capture_wait", "timeout", target=target, timeout_seconds=args.timeout_seconds)
            raise SystemExit(str(e))
    else:
        p = tmux_capture(target, args.lines)
        if p.returncode != 0:
            append_ledger(rd, "capture", "fail", target=target, error=p.stderr.strip())
            raise SystemExit(p.stderr.strip())
        text = p.stdout
    out = rd / f"GJC_LOG.{dt.datetime.now().strftime('%H%M%S')}.md"
    out.write_text("# GJC Captured Log\n\n```text\n" + text + "\n```\n", encoding="utf-8")
    append_ledger(rd, "capture", "done", target=target, path=str(out), waited_for_verdict=wait_for_verdict)
    print(f"Captured tmux log: {out}")
    if wait_for_verdict and is_awaiting_reviewer:
        iteration = int(awaiting.get("iteration") or args.iteration or 1)
        review_path = rd / extract_review_result_name(iteration)
        review_path.write_text(text, encoding="utf-8")
        parsed = classify_review_file(root, run, rd, review_path)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        run_after, rd_after = active_run(root)
        continue_after_review_classification(root, run_after, rd_after, iteration, parsed)
    elif is_awaiting_executor and not args.tmux_target:
        iteration = int(awaiting.get("iteration") or args.iteration or 1)
        continue_after_executor_capture(root, run, rd, iteration)


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
        target = args.tmux_target or run.get("runner", {}).get("reviewer_target") or run.get("runner", {}).get("target") or "gjc:0.0"
        tmux_send(target, prompt)
        run["awaiting"] = {"phase": "reviewer", "target": target, "prompt": str(path), "since": now_iso()}
        write_json(rd / "RUN.json", run)
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
        learn_md, learn_candidates, applied_candidates = run_learn(
            root,
            run,
            rd,
            apply=getattr(args, "apply_learn", False),
            auto_apply_high=os.environ.get("CFC_DONE_AUTO_APPLY_HIGH_LEARN", "1") not in {"0", "false", "False", "no"},
        )
        if applied_candidates:
            print(f"Applied {len(applied_candidates)} high-confidence learn candidate(s) to .cfc/wiki")
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
    verdict = verdict_match.group(1).upper() if verdict_match else "REVIEW_BLOCKED"

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
    if not verdict_match:
        blockers = ["review missing required final Verdict line"] + blockers
    elif verdict == "REVIEW_BLOCKED" and not blockers:
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


def run_agent_command(command: str, prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), input=prompt, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, timeout=timeout)


def classify_review_file(root: Path, run: dict[str, Any], rd: Path, path: Path) -> dict[str, Any]:
    parsed = parse_review_result(path.read_text(encoding="utf-8", errors="ignore"))
    (rd / "BLOCKERS.md").write_text(render_blockers_md(path, parsed), encoding="utf-8")
    run["review"] = {"verdict": parsed["verdict"], "blockers": parsed["blockers"], "review_file": str(path), "classified_at": now_iso()}
    if run.get("awaiting", {}).get("phase") == "reviewer":
        run.pop("awaiting", None)
    write_json(rd / "RUN.json", run)
    append_ledger(rd, "review_classify", parsed["verdict"].lower(), blocker_count=len(parsed["blockers"]), review_file=str(path))
    learn_md, candidates, applied = run_learn(root, run, rd, auto_apply_high=True)
    append_ledger(rd, "learn_after_review", "done", candidate_count=len(candidates), applied_count=len(applied), review_verdict=parsed["verdict"])
    return parsed


def cmd_classify_review(args: argparse.Namespace) -> None:
    root = root_path(args)
    run, rd = active_run(root)
    path = Path(args.review_file).expanduser().resolve() if args.review_file else latest_review_result(rd)[0]
    if not path or not path.exists():
        raise SystemExit("No REVIEW.iteration-*.md found. Pass --review-file or run cfc review/cfc loop first.")
    parsed = classify_review_file(root, run, rd, path)
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
        next_iteration = args.iteration + 1
        run["awaiting"] = {"phase": "executor", "iteration": next_iteration, "target": target, "prompt": str(path), "since": now_iso(), "source_review_iteration": args.iteration}
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "repair_send", "sent", iteration=args.iteration, next_iteration=next_iteration, target=target, prompt=str(path))
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
    if args.send and getattr(args, "isolated_tmux", False):
        args.executor_target, args.reviewer_target = ensure_isolated_tmux_targets(root, run, rd)
        run, rd = active_run(root)
    run.setdefault("loop", {})["max_iterations"] = args.max_iterations
    run["loop"]["review_on_check_fail"] = bool(args.review_on_check_fail)
    if args.send:
        run.setdefault("runner", {})["target"] = args.executor_target
        run["runner"]["reviewer_target"] = args.reviewer_target
    write_json(rd / "RUN.json", run)
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
            if not args.tmux_wait_seconds:
                run["awaiting"] = {"phase": "executor", "iteration": iteration, "target": args.executor_target, "prompt": str(prompt_path), "since": now_iso()}
                write_json(rd / "RUN.json", run)
                append_ledger(rd, "loop", "waiting_for_executor", iteration=iteration, target=args.executor_target)
                print(f"CfC dispatched executor prompt to {args.executor_target} and is waiting for external completion before check/review.")
                print(f"Run dir: {rd}")
                return
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
                if not args.tmux_wait_seconds:
                    run["awaiting"] = {"phase": "reviewer", "iteration": iteration, "target": args.reviewer_target, "prompt": str(review_prompt_path), "since": now_iso()}
                    write_json(rd / "RUN.json", run)
                    append_ledger(rd, "loop", "waiting_for_reviewer", iteration=iteration, target=args.reviewer_target)
                    print(f"CfC dispatched reviewer prompt to {args.reviewer_target} and is waiting for external completion before classification.")
                    print(f"Run dir: {rd}")
                    return
                review_text = wait_for_tmux_verdict(
                    args.reviewer_target,
                    args.capture_lines,
                    poll_seconds=float(os.environ.get("CFC_REVIEW_POLL_SECONDS", "5")),
                    timeout_seconds=int(os.environ.get("CFC_REVIEW_WAIT_TIMEOUT_SECONDS", str(args.tmux_wait_seconds or 0))),
                )
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




def run_summary(root: Path) -> dict[str, Any]:
    if not is_git_repo(root):
        return non_repo_payload(root)
    initialized = cfc_path(root).exists()
    active: tuple[dict[str, Any], Path] | None = None
    if initialized:
        try:
            active = current_active_run_or_none(root)
        except Exception:
            active = None
    changed = parse_status_files(git_status_short(root))
    payload: dict[str, Any] = {
        "version": VERSION,
        "repo": str(root),
        "is_git_repo": True,
        "branch": git_branch(root),
        "dirty": bool(changed),
        "changed_files": changed,
        "initialized": initialized,
        "active_run": None,
    }
    if active:
        run, rd = active
        ledger_events: list[dict[str, Any]] = []
        ledger = rd / "ledger.jsonl"
        if ledger.exists():
            for line in ledger.read_text(encoding="utf-8", errors="ignore").splitlines()[-8:]:
                try:
                    ledger_events.append(json.loads(line))
                except json.JSONDecodeError:
                    ledger_events.append({"raw": line})
        payload["active_run"] = {
            "id": run.get("id"),
            "title": run.get("title"),
            "status": run.get("status"),
            "run_dir": str(rd),
            "check": run.get("check", {}),
            "review": run.get("review", {}),
            "recent_events": ledger_events,
        }
    return payload


def print_headless_help() -> None:
    print(f"""CfC {VERSION} — headless recursive agent controller

Usage:
  cfc plugin manifest
  cfc plugin run "task" --root /path/to/repo [--replace] [--allow-dirty]
  cfc plugin status --root /path/to/repo
  cfc loop --root /path/to/repo "task" --executor-command ... --reviewer-command ...
  cfc "task" --root /path/to/repo

CfC no longer opens an interactive TUI. It is meant to be called by Codex/OMX/GJC/other plugin adapters.
Core loop: executor adapter -> git/check evidence -> independent reviewer adapter -> repair -> learn.
""")


def known_commands() -> set[str]:
    return {
        "init", "start", "status", "gjc", "capture", "check", "diff", "review",
        "classify-review", "repair", "loop", "park", "learn", "done", "events", "plugin",
    }


def default_loop_namespace(request: str, root: str = ".", replace: bool = False, allow_dirty: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        root=root,
        request=request,
        allow=["*"],
        forbid=None,
        verify=["git diff --check"],
        max_iterations=int(os.environ.get("CFC_MAX_ITERATIONS", "3")),
        executor_target=os.environ.get("CFC_EXECUTOR_TARGET", "gjc:0.0"),
        reviewer_target=os.environ.get("CFC_REVIEWER_TARGET", "cfc-review:0.0"),
        send=os.environ.get("CFC_SEND", "1") not in {"0", "false", "False", "no"},
        tmux_wait_seconds=int(os.environ.get("CFC_TMUX_WAIT_SECONDS", "0")),
        capture_lines=int(os.environ.get("CFC_CAPTURE_LINES", "5000")),
        isolated_tmux=os.environ.get("CFC_ISOLATED_TMUX", "1") not in {"0", "false", "False", "no"},
        executor_command=os.environ.get("CFC_EXECUTOR_COMMAND") or None,
        reviewer_command=os.environ.get("CFC_REVIEWER_COMMAND") or None,
        timeout=int(os.environ.get("CFC_TIMEOUT", "600")),
        allow_dirty=allow_dirty,
        replace=replace,
        apply_learn=os.environ.get("CFC_APPLY_LEARN", "0") in {"1", "true", "True", "yes"},
        review_on_check_fail=True,
    )


def run_bare_request(argv: list[str]) -> int:
    request_parts: list[str] = []
    root = "."
    replace = False
    allow_dirty = False
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
        if arg == "--allow-dirty":
            allow_dirty = True
            i += 1
            continue
        request_parts.append(arg)
        i += 1
    root_path_value = str(nearest_git_root(Path(root)))
    request = " ".join(request_parts).strip()
    if not request:
        print_headless_help()
        return 0
    dirty_files = parse_status_files(git_status_short(Path(root_path_value))) if (Path(root_path_value) / ".git").exists() else []
    auto_allow_dirty = allow_dirty or looks_like_cfc_dev_workspace(Path(root_path_value), dirty_files)
    cmd_loop(default_loop_namespace(request, root=root_path_value, replace=replace, allow_dirty=auto_allow_dirty))
    return 0


def cmd_plugin_manifest(args: argparse.Namespace) -> None:
    manifest = {
        "name": "cfc",
        "version": VERSION,
        "description": "Headless recursive controller for Codex/OMX/GJC-style agent plugins.",
        "interface": "stdio-cli",
        "commands": {
            "run": "Start/replace a recursive loop for a task.",
            "status": "Return machine-readable repo/run status.",
            "events": "Return recent active-run ledger events.",
            "cancel": "Clear the active run pointer without deleting artifacts.",
        },
        "env": [
            "CFC_EXECUTOR_COMMAND", "CFC_REVIEWER_COMMAND", "CFC_EXECUTOR_TARGET", "CFC_REVIEWER_TARGET",
            "CFC_SEND", "CFC_TMUX_WAIT_SECONDS", "CFC_MAX_ITERATIONS", "CFC_APPLY_LEARN", "CFC_ISOLATED_TMUX",
            "CFC_DONE_AUTO_APPLY_HIGH_LEARN", "CFC_REVIEW_POLL_SECONDS", "CFC_REVIEW_WAIT_TIMEOUT_SECONDS",
        ],
    }
    print(json.dumps(manifest, indent=2, ensure_ascii=False))


def cmd_plugin_status(args: argparse.Namespace) -> None:
    requested = root_path(args)
    root = nearest_git_root(requested)
    print(json.dumps(run_summary(root if is_git_repo(root) else requested), indent=2, ensure_ascii=False))


def cmd_plugin_events(args: argparse.Namespace) -> None:
    root = resolve_plugin_root(args)
    run, rd = active_run(root)
    path = rd / "ledger.jsonl"
    events: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-args.limit:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"raw": line})
    print(json.dumps({"run_id": run.get("id"), "events": events}, indent=2, ensure_ascii=False))


def cmd_plugin_cancel(args: argparse.Namespace) -> None:
    root = resolve_plugin_root(args)
    run, rd = active_run(root)
    run["status"] = "cancelled"
    run["completed_at"] = now_iso()
    write_json(rd / "RUN.json", run)
    write_json(current_file(root), {"run_id": None, "last_run_id": run["id"], "updated_at": now_iso(), "cancelled": True})
    append_ledger(rd, "cancel", "cancelled")
    print(json.dumps({"cancelled": True, "run_id": run.get("id"), "run_dir": str(rd)}, indent=2, ensure_ascii=False))


def cmd_plugin_run(args: argparse.Namespace) -> None:
    root_path_value = resolve_plugin_root(args)
    root = str(root_path_value)
    ns = default_loop_namespace(args.request, root=root, replace=args.replace, allow_dirty=args.allow_dirty)
    if args.executor_command:
        ns.executor_command = args.executor_command
        ns.send = False
    if args.reviewer_command:
        ns.reviewer_command = args.reviewer_command
        ns.send = False
    if args.executor_target:
        ns.executor_target = args.executor_target
        ns.isolated_tmux = False
    if args.reviewer_target:
        ns.reviewer_target = args.reviewer_target
        ns.isolated_tmux = False
    if getattr(args, "isolated_tmux", False):
        ns.isolated_tmux = True
    if args.no_send:
        ns.send = False
    if args.max_iterations is not None:
        ns.max_iterations = args.max_iterations
    if args.verify:
        ns.verify = args.verify
    if args.allow:
        ns.allow = args.allow
    if args.forbid:
        ns.forbid = args.forbid
    cmd_loop(ns)
    print(json.dumps(run_summary(Path(root)), indent=2, ensure_ascii=False))

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
    sp.add_argument("--wait-verdict", action="store_true", help="Wait until captured tmux output contains final Verdict: PASS/REVIEW_BLOCKED")
    sp.add_argument("--no-wait-verdict", action="store_true", help="Do not auto-wait even when awaiting reviewer")
    sp.add_argument("--poll-seconds", type=float, default=float(os.environ.get("CFC_REVIEW_POLL_SECONDS", "5")))
    sp.add_argument("--timeout-seconds", type=int, default=int(os.environ.get("CFC_REVIEW_WAIT_TIMEOUT_SECONDS", "0")), help="0 means wait indefinitely")
    sp.add_argument("--iteration", type=int)
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
    sp.add_argument("--isolated-tmux", action="store_true", help="Create dedicated executor/reviewer GJC tmux sessions for this run")
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
    sp.add_argument("--no-auto-learn", action="store_true", help="Skip automatic LEARN.md generation before marking done")
    sp.add_argument("--apply-learn", action="store_true", help="Apply all learn candidates to .cfc/wiki before marking done")
    sp.set_defaults(func=cmd_done)

    plugin = sub.add_parser("plugin", help="Machine-readable adapter surface for Codex/OMX/GJC plugins")
    plugin_sub = plugin.add_subparsers(dest="plugin_cmd", required=True)

    sp = plugin_sub.add_parser("manifest")
    sp.set_defaults(func=cmd_plugin_manifest)

    sp = plugin_sub.add_parser("status")
    add_root(sp)
    sp.set_defaults(func=cmd_plugin_status)

    sp = plugin_sub.add_parser("events")
    add_root(sp)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_plugin_events)

    sp = plugin_sub.add_parser("cancel")
    add_root(sp)
    sp.set_defaults(func=cmd_plugin_cancel)

    sp = plugin_sub.add_parser("run")
    add_root(sp)
    sp.add_argument("request")
    sp.add_argument("--allow", action="append")
    sp.add_argument("--forbid", action="append")
    sp.add_argument("--verify", action="append")
    sp.add_argument("--max-iterations", type=int)
    sp.add_argument("--executor-target")
    sp.add_argument("--reviewer-target")
    sp.add_argument("--isolated-tmux", action="store_true", help="Create dedicated executor/reviewer GJC tmux sessions for this run")
    sp.add_argument("--executor-command")
    sp.add_argument("--reviewer-command")
    sp.add_argument("--no-send", action="store_true")
    sp.add_argument("--allow-dirty", action="store_true")
    sp.add_argument("--replace", action="store_true")
    sp.set_defaults(func=cmd_plugin_run)

    sp = sub.add_parser("events")
    add_root(sp)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_events)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        return run_bare_request([])
    if argv[0] == "chat":
        print("cfc chat/TUI mode was removed. Use: cfc plugin run/status/events/cancel", file=sys.stderr)
        return 2
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






