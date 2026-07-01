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

from .common import append_ledger, now_iso, sha256_text, slugify, write_json


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "") in {"1", "true", "True", "yes", "on"}

def tmux_send(target: str, text: str) -> None:
    # paste-buffer is safer than send-keys for multiline prompts.
    # Use load-buffer via stdin instead of `set-buffer <text>` so large CfC
    # prompts do not hit the OS argv/ARG_MAX limit ("command too long").
    subprocess.run(["tmux", "load-buffer", "-"], input=text, text=True, check=True)
    subprocess.run(["tmux", "paste-buffer", "-t", target], check=True)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)

def send_tmux_prompt(run: dict[str, Any], rd: Path, ledger_phase: str, target: str, text: str, **fields: Any) -> None:
    try:
        tmux_send(target, text)
    except Exception as exc:
        run["status"] = "send_failed"
        # A failed send must not leave a stale awaiting pointer from a prior
        # successful dispatch; clear it atomically with the send_failed status.
        run.pop("awaiting", None)
        run["send_error"] = {
            "phase": ledger_phase,
            "target": target,
            "error": str(exc),
            "at": now_iso(),
        }
        write_json(rd / "RUN.json", run)
        append_ledger(rd, ledger_phase, "fail", target=target, error=str(exc), **fields)
        cleanup_isolated_tmux_sessions(run, rd, reason=f"{ledger_phase}_failed", force=True)
        raise SystemExit(f"Failed to send {ledger_phase} prompt to tmux target {target}: {exc}") from exc
    append_ledger(rd, ledger_phase, "sent", target=target, **fields)

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

def render_reviewer_timeout_result(target: str, timeout_seconds: int, captured_text: str) -> str:
    timeout_label = f"{timeout_seconds}s" if timeout_seconds else "the configured wait"
    excerpt = captured_text[-12000:]
    return f"""Verdict: REVIEW_BLOCKED

## BLOCKERS
- reviewer did not complete within {timeout_label} waiting for final Verdict: PASS or Verdict: REVIEW_BLOCKED from {target}; review evidence is incomplete

## MAJOR
- none

## MINOR
- none

## Verification gaps
- reviewer output was incomplete, so CfC converted the timeout into a blocked review artifact instead of waiting indefinitely

## Suggested repair prompt
- none; resolve reviewer timeout/scope before asking the executor to repair product code

## Captured reviewer output excerpt

```text
{excerpt}
```
"""

def short_run_token(run_id: str) -> str:
    head = run_id.split("-", 2)
    prefix = "-".join(head[:2]) if len(head) >= 2 else slugify(run_id)[:15]
    digest = sha256_text(run_id)[:16]
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


def tmux_kill_session(session: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["tmux", "kill-session", "-t", session], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def cleanup_isolated_tmux_sessions(run: dict[str, Any], rd: Path, reason: str, force: bool = False) -> list[dict[str, Any]]:
    """Kill CFC-owned isolated tmux sessions once they are no longer awaited.

    User-provided targets such as ``gjc:0.0`` are not touched. Only sessions
    created by ``ensure_isolated_tmux_targets`` are eligible.
    """
    runner = run.get("runner") if isinstance(run.get("runner"), dict) else {}
    if not runner.get("isolated_tmux"):
        return []
    if _env_truthy("CFC_KEEP_ISOLATED_TMUX"):
        append_ledger(rd, "tmux_isolated_cleanup", "skipped", reason=reason, keep_env=True)
        return []
    if run.get("awaiting") and not force:
        append_ledger(rd, "tmux_isolated_cleanup", "skipped", reason=reason, awaiting=run.get("awaiting"))
        return []
    if runner.get("isolated_tmux_cleaned_at"):
        return []

    sessions: list[str] = []
    for key in ["executor_session", "reviewer_session"]:
        value = runner.get(key)
        if isinstance(value, str) and value and value not in sessions:
            sessions.append(value)

    results: list[dict[str, Any]] = []
    for session in sessions:
        res = tmux_kill_session(session)
        result = {
            "session": session,
            "exit_code": res.returncode,
            "stderr": (res.stderr or "").strip(),
        }
        results.append(result)
        append_ledger(
            rd,
            "tmux_isolated_cleanup",
            "done" if res.returncode == 0 else "miss",
            reason=reason,
            session=session,
            exit_code=res.returncode,
            stderr=result["stderr"],
        )
    runner["isolated_tmux_cleaned_at"] = now_iso()
    runner["isolated_tmux_cleanup_reason"] = reason
    runner["isolated_tmux_cleanup"] = results
    write_json(rd / "RUN.json", run)
    return results
