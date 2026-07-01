from __future__ import annotations

import argparse
import os
import re
import shlex
from pathlib import Path
from typing import Any

TRUE_STRINGS = {"1", "true", "True", "yes", "on"}
FALSE_STRINGS = {"0", "false", "False", "no", "off", "none", ""}

LIVE_ADAPTER_PATTERNS = [
    ("gjc", re.compile(r"(?<![\w./-])(?:[\w./-]+/)?gjc(?:\s|$)")),
    ("opencode", re.compile(r"(?<![\w./-])(?:[\w./-]+/)?opencode(?:\s|$)")),
    ("codex exec", re.compile(r"(?<![\w./-])(?:[\w./-]+/)?codex\s+exec(?:\s|$)")),
]

def env_truthy(name: str) -> bool:
    return os.environ.get(name, "") in TRUE_STRINGS

def codex_sandbox_active() -> bool:
    value = os.environ.get("CODEX_SANDBOX", "")
    return bool(value) and value not in FALSE_STRINGS

def live_adapter_kind(command: str) -> str | None:
    for label, pattern in LIVE_ADAPTER_PATTERNS:
        if pattern.search(command):
            return label
    return None

def live_adapter_attempts(args: argparse.Namespace) -> list[dict[str, str]]:
    attempts: list[dict[str, str]] = []
    command = getattr(args, "executor_command", None)
    if command:
        kind = live_adapter_kind(str(command))
        if kind:
            attempts.append({
                "phase": "executor",
                "profile": str(getattr(args, "executor_profile", None) or "custom"),
                "kind": kind,
                "command": str(command),
            })
    for fallback in getattr(args, "executor_fallbacks", []) or []:
        profile = "fallback"
        command = None
        if isinstance(fallback, dict):
            profile = str(fallback.get("profile") or profile)
            command = fallback.get("command")
        elif isinstance(fallback, (list, tuple)) and len(fallback) >= 2:
            profile = str(fallback[0] or profile)
            command = fallback[1]
        elif isinstance(fallback, str):
            command = fallback
        if not command:
            continue
        kind = live_adapter_kind(str(command))
        if kind:
            attempts.append({
                "phase": "executor_fallback",
                "profile": profile,
                "kind": kind,
                "command": str(command),
            })
    command = getattr(args, "reviewer_command", None)
    if command:
        kind = live_adapter_kind(str(command))
        if kind:
            attempts.append({
                "phase": "reviewer",
                "profile": str(getattr(args, "reviewer_profile", None) or "custom"),
                "kind": kind,
                "command": str(command),
            })
    return attempts

def external_terminal_command(root: Path, args: argparse.Namespace) -> str:
    parts = [
        "cd",
        shlex.quote(str(root)),
        "&&",
        "env",
        "-u",
        "CODEX_SANDBOX",
        "cfc",
        "plugin",
        "run",
        shlex.quote(str(getattr(args, "request", ""))),
        "--root",
        shlex.quote(str(root)),
    ]
    if getattr(args, "replace", False):
        parts.append("--replace")
    if getattr(args, "allow_dirty", False):
        parts.append("--allow-dirty")
    if getattr(args, "budget", None):
        parts.extend(["--budget", shlex.quote(str(args.budget))])
    max_iterations = getattr(args, "max_iterations", None)
    if max_iterations is not None:
        parts.extend(["--max-iterations", shlex.quote(str(max_iterations))])
    if getattr(args, "executor_profile", None):
        parts.extend(["--executor-profile", shlex.quote(str(args.executor_profile))])
    if getattr(args, "reviewer_profile", None):
        parts.extend(["--reviewer-profile", shlex.quote(str(args.reviewer_profile))])
    if getattr(args, "isolated_tmux", False):
        parts.append("--isolated-tmux")
    if getattr(args, "executor_tmux_command", None):
        parts.extend(["--executor-tmux-command", shlex.quote(str(args.executor_tmux_command))])
    if getattr(args, "reviewer_tmux_command", None):
        parts.extend(["--reviewer-tmux-command", shlex.quote(str(args.reviewer_tmux_command))])
    if getattr(args, "review_on_check_fail", True) is False:
        parts.append("--no-review-on-check-fail")
    if getattr(args, "review_risk_gate", None) is True:
        parts.append("--review-risk-gate")
    elif getattr(args, "review_risk_gate", None) is False:
        parts.append("--no-review-risk-gate")
    for value in getattr(args, "verify", []) or []:
        parts.extend(["--verify", shlex.quote(str(value))])
    for value in getattr(args, "allow", []) or []:
        parts.extend(["--allow", shlex.quote(str(value))])
    for value in getattr(args, "forbid", []) or []:
        parts.extend(["--forbid", shlex.quote(str(value))])
    return " ".join(parts)

def external_terminal_block_message(root: Path, args: argparse.Namespace, attempts: list[dict[str, str]]) -> str:
    attempt_lines = "\n".join(
        f"- {item['phase']} ({item['profile']}): {item['command']}"
        for item in attempts
    )
    command = external_terminal_command(root, args)
    return (
        "CfC live command adapters are disabled inside the Codex App sandbox.\n\n"
        "Detected CODEX_SANDBOX and live adapter command(s) that normally write "
        "machine-local state such as ~/.gjc logs or ~/.codex SQLite state:\n"
        f"{attempt_lines}\n\n"
        "Run this loop from an external terminal or tmux pane instead:\n\n"
        f"  {command}\n\n"
        "If you intentionally want to run live adapters inside this environment, "
        "set CFC_ALLOW_SANDBOX_LIVE_ADAPTERS=1."
    )

def external_terminal_handoff_payload(root: Path, args: argparse.Namespace) -> dict[str, Any]:
    attempts = live_adapter_attempts(args)
    sandbox_active = codex_sandbox_active()
    bypassed = env_truthy("CFC_ALLOW_SANDBOX_LIVE_ADAPTERS")
    return {
        "handoff_required": sandbox_active and bool(attempts) and not bypassed,
        "reason": "codex_app_sandbox_live_adapters" if sandbox_active and attempts and not bypassed else "not_required",
        "repo": str(root),
        "request": str(getattr(args, "request", "")),
        "sandbox_active": sandbox_active,
        "allow_sandbox_live_adapters": bypassed,
        "live_adapter_attempts": attempts,
        "external_command": external_terminal_command(root, args),
    }

def enforce_external_terminal_for_live_adapters(args: argparse.Namespace, root: Path) -> None:
    if getattr(args, "send", False):
        return
    if not codex_sandbox_active() or env_truthy("CFC_ALLOW_SANDBOX_LIVE_ADAPTERS"):
        return
    attempts = live_adapter_attempts(args)
    if not attempts:
        return
    raise SystemExit(external_terminal_block_message(root, args, attempts))
