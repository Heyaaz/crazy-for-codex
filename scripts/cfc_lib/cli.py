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

from .commands_core import cmd_check, cmd_diff, cmd_done, cmd_events, cmd_init, cmd_park, cmd_review, cmd_start, cmd_status
from .constants import TRACKED_CONFIG_FILE, VERSION
from .git_ops import nearest_git_root
from .hooks import cmd_hook_stop, cmd_hook_subagent_stop, cmd_hook_user_prompt_submit
from .learn import cmd_learn
from .loop import cmd_capture, cmd_gjc, cmd_loop, default_loop_namespace
from .plugin import cmd_plugin_cancel, cmd_plugin_events, cmd_plugin_manifest, cmd_plugin_run, cmd_plugin_status
from .review_workflow import cmd_classify_review, cmd_repair

def print_headless_help() -> None:
    print(f"""CfC {VERSION} — headless recursive agent controller

Usage:
  cfc plugin manifest
  cfc plugin run "task" --root /path/to/repo [--replace] [--allow-dirty]
  cfc plugin run "task" --root /path/to/repo --handoff-only
  cfc plugin status --root /path/to/repo
  cfc hook user-prompt-submit|stop|subagent-stop --root /path/to/repo
  cfc loop --root /path/to/repo "task" --executor-command ... --reviewer-command ...
  cfc "task" --root /path/to/repo

CfC no longer opens an interactive TUI. It is meant to be called by Codex/OMX/GJC/other plugin adapters.
Core loop: executor adapter -> git/check evidence -> independent reviewer adapter -> repair -> learn.
Tracked config: `{TRACKED_CONFIG_FILE}` can define command-mode executor/reviewer profiles.
""")

def known_commands() -> set[str]:
    return {
        "init", "start", "status", "gjc", "capture", "check", "diff", "review",
        "classify-review", "repair", "loop", "park", "learn", "done", "events", "plugin", "hook",
    }

def run_bare_request(argv: list[str]) -> int:
    request_parts: list[str] = []
    root = "."
    replace = False
    allow_dirty = False
    budget = None
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
        if arg == "--budget" and i + 1 < len(argv):
            budget = argv[i + 1]
            i += 2
            continue
        request_parts.append(arg)
        i += 1
    root_path_value = str(nearest_git_root(Path(root)))
    request = " ".join(request_parts).strip()
    if not request:
        print_headless_help()
        return 0
    cmd_loop(default_loop_namespace(request, root=root_path_value, replace=replace, allow_dirty=allow_dirty, budget=budget))
    return 0

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cfc", description="CfC recursive GJC harness")
    p.add_argument("--version", action="version", version=f"CfC {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_root(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--root", default=".", help="Target repository root")

    def add_budget(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--budget", choices=["light", "normal", "strict"], help="Token/context budget preset")

    sp = sub.add_parser("init")
    add_root(sp)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("start")
    add_root(sp)
    add_budget(sp)
    sp.add_argument("title")
    sp.add_argument("--allow", action="append")
    sp.add_argument("--forbid", action="append")
    sp.add_argument("--verify", action="append")
    sp.add_argument("--tmux-target")
    sp.add_argument("--max-iterations", type=int, default=None, help="Persist loop max_iterations for async continuation (default: config/env/3)")
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
    sp.add_argument("--lines", type=int, default=None)
    sp.add_argument("--wait-verdict", action="store_true", help="Wait until captured tmux output contains final Verdict: PASS/REVIEW_BLOCKED")
    sp.add_argument("--no-wait-verdict", action="store_true", help="Do not auto-wait even when awaiting reviewer")
    sp.add_argument("--poll-seconds", type=float, default=float(os.environ.get("CFC_REVIEW_POLL_SECONDS", "5")))
    sp.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.environ.get("CFC_REVIEW_WAIT_TIMEOUT_SECONDS", "300")),
        help="Seconds to wait for a reviewer Verdict before writing REVIEW_BLOCKED; pass 0 to explicitly wait indefinitely",
    )
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
    add_budget(sp)
    sp.add_argument("request")
    sp.add_argument("--allow", action="append")
    sp.add_argument("--forbid", action="append")
    sp.add_argument("--verify", action="append")
    sp.add_argument("--max-iterations", type=int, default=3)
    sp.add_argument("--executor-target", default="gjc:0.0")
    sp.add_argument("--reviewer-target", default="gjc:0.1")
    sp.add_argument("--send", action="store_true")
    sp.add_argument("--tmux-wait-seconds", type=int, default=0)
    sp.add_argument("--capture-lines", type=int, default=None)
    sp.add_argument("--isolated-tmux", action="store_true", help="Create dedicated executor/reviewer GJC tmux sessions for this run")
    sp.add_argument("--executor-profile")
    sp.add_argument("--reviewer-profile")
    sp.add_argument("--executor-command")
    sp.add_argument("--reviewer-command")
    sp.add_argument("--timeout", type=int, default=600)
    sp.add_argument("--allow-dirty", action="store_true")
    sp.add_argument("--replace", action="store_true")
    sp.add_argument("--apply-learn", action="store_true")
    review_fail = sp.add_mutually_exclusive_group()
    review_fail.add_argument("--review-on-check-fail", dest="review_on_check_fail", action="store_true", default=True)
    review_fail.add_argument("--no-review-on-check-fail", dest="review_on_check_fail", action="store_false")
    review_gate = sp.add_mutually_exclusive_group()
    review_gate.add_argument("--review-risk-gate", dest="review_risk_gate", action="store_true", default=None, help="Skip reviewer for CHECK PASS low-risk/no-diff runs")
    review_gate.add_argument("--no-review-risk-gate", dest="review_risk_gate", action="store_false", help="Always call the reviewer adapter")
    sp.set_defaults(func=cmd_loop)

    sp = sub.add_parser("park")
    add_root(sp)
    sp.add_argument("note")
    sp.set_defaults(func=cmd_park)

    sp = sub.add_parser("learn")
    add_root(sp)
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("--promote-global", action="store_true", help="Promote safe global-scope learn candidates to the global CFC wiki")
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
    add_budget(sp)
    sp.add_argument("request")
    sp.add_argument("--allow", action="append")
    sp.add_argument("--forbid", action="append")
    sp.add_argument("--verify", action="append")
    sp.add_argument("--max-iterations", type=int)
    sp.add_argument("--executor-target")
    sp.add_argument("--reviewer-target")
    sp.add_argument("--isolated-tmux", action="store_true", help="Create dedicated executor/reviewer GJC tmux sessions for this run")
    sp.add_argument("--executor-profile")
    sp.add_argument("--reviewer-profile")
    sp.add_argument("--executor-command")
    sp.add_argument("--reviewer-command")
    sp.add_argument("--no-send", action="store_true")
    sp.add_argument("--allow-dirty", action="store_true")
    sp.add_argument("--replace", action="store_true")
    sp.add_argument("--no-review-on-check-fail", action="store_true")
    review_gate = sp.add_mutually_exclusive_group()
    review_gate.add_argument("--review-risk-gate", dest="review_risk_gate", action="store_true", default=None)
    review_gate.add_argument("--no-review-risk-gate", dest="review_risk_gate", action="store_false")
    sp.add_argument("--handoff-only", action="store_true", help="Print external-terminal handoff JSON without starting a run")
    sp.set_defaults(func=cmd_plugin_run)

    hook = sub.add_parser("hook", help="Thin hook shim for routing and recursive-assurance guards")
    hook_sub = hook.add_subparsers(dest="hook_cmd", required=True)

    sp = hook_sub.add_parser("user-prompt-submit", help="Emit strict CFC router context only for explicit CFC prompts")
    add_root(sp)
    sp.add_argument("--json", action="store_true", help="Emit machine-readable hook decision JSON")
    sp.set_defaults(func=cmd_hook_user_prompt_submit)

    sp = hook_sub.add_parser("stop", help="Block stopping when an active CFC run is unresolved")
    add_root(sp)
    sp.add_argument("--json", action="store_true", help="Emit machine-readable hook decision JSON")
    sp.set_defaults(func=cmd_hook_stop)

    sp = hook_sub.add_parser("subagent-stop", help="Validate CFC evidence receipts for active strict runs")
    add_root(sp)
    sp.add_argument("--json", action="store_true", help="Emit machine-readable hook decision JSON")
    sp.add_argument("--strict", action="store_true", help="Require an evidence receipt even if the active run did not opt in")
    sp.set_defaults(func=cmd_hook_subagent_stop)

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
