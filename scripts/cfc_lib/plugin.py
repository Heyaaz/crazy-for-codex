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

from .common import append_ledger, now_iso, read_json, write_json
from .config import apply_configured_adapters
from .constants import TRACKED_CONFIG_FILE, VERSION
from .git_ops import git_branch, git_status_short, is_git_repo, nearest_git_root, non_repo_payload, parse_status_files, resolve_plugin_root
from .loop import cmd_loop, default_loop_namespace
from .paths import cfc_path, current_file, root_path
from .state import active_run, current_active_run_or_none

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
            "awaiting": run.get("awaiting"),
            "send_error": run.get("send_error"),
            "check": run.get("check", {}),
            "review": run.get("review", {}),
            "recent_events": ledger_events,
        }
    return payload

def cmd_plugin_manifest(args: argparse.Namespace) -> None:
    manifest = {
        "name": "cfc",
        "version": VERSION,
        "description": "Headless recursive controller for Codex/OMX/GJC-style agent plugins.",
        "interface": "stdio-cli",
        "config_file": TRACKED_CONFIG_FILE,
        "commands": {
            "run": "Start/replace a recursive loop for a task.",
            "status": "Return machine-readable repo/run status.",
            "events": "Return recent active-run ledger events.",
            "cancel": "Clear the active run pointer without deleting artifacts.",
        },
        "config": {
            "adapters": "Use adapters.mode=command plus executor_profile/reviewer_profile/profiles for cost-optimized model routing.",
        },
        "env": [
            "CFC_EXECUTOR_COMMAND", "CFC_REVIEWER_COMMAND", "CFC_EXECUTOR_TARGET", "CFC_REVIEWER_TARGET",
            "CFC_SEND", "CFC_TMUX_WAIT_SECONDS", "CFC_MAX_ITERATIONS", "CFC_APPLY_LEARN", "CFC_ISOLATED_TMUX",
            "CFC_DONE_AUTO_APPLY_HIGH_LEARN", "CFC_REVIEW_AUTO_APPLY_HIGH_LEARN", "CFC_REVIEW_ON_CHECK_FAIL",
            "CFC_REVIEW_POLL_SECONDS", "CFC_REVIEW_WAIT_TIMEOUT_SECONDS", "CFC_ALLOW_SANDBOX_LIVE_ADAPTERS",
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
    run["cancelled_at"] = run["completed_at"]
    run.pop("awaiting", None)
    write_json(rd / "RUN.json", run)
    write_json(current_file(root), {"run_id": None, "last_run_id": run["id"], "updated_at": now_iso(), "cancelled": True})
    append_ledger(rd, "cancel", "cancelled")
    print(json.dumps({"cancelled": True, "run_id": run.get("id"), "run_dir": str(rd)}, indent=2, ensure_ascii=False))

def cmd_plugin_run(args: argparse.Namespace) -> None:
    root_path_value = resolve_plugin_root(args)
    root = str(root_path_value)
    ns = default_loop_namespace(args.request, root=root, replace=args.replace, allow_dirty=args.allow_dirty)
    if getattr(args, "executor_profile", None):
        ns.executor_profile = args.executor_profile
        ns.executor_command = None
    if getattr(args, "reviewer_profile", None):
        ns.reviewer_profile = args.reviewer_profile
        ns.reviewer_command = None
    apply_configured_adapters(ns, root_path_value)
    if args.executor_command:
        ns.executor_command = args.executor_command
        ns.executor_profile = None
        ns.executor_fallbacks = []
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
    if getattr(args, "no_review_on_check_fail", False):
        ns.review_on_check_fail = False
    if args.verify:
        ns.verify = args.verify
    if args.allow:
        ns.allow = args.allow
    if args.forbid:
        ns.forbid = args.forbid
    cmd_loop(ns)
    print(json.dumps(run_summary(Path(root)), indent=2, ensure_ascii=False))
