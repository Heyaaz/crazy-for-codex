#!/usr/bin/env python3
"""CfC Recursive Harness CLI entrypoint.

Implementation is split by feature under ``scripts/cfc_lib``.
This file stays as a thin executable wrapper plus compatibility re-export for
older tests/importers that load ``scripts/cfc.py`` directly.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from cfc_lib import *  # noqa: F401,F403
from cfc_lib import cli as _cli
from cfc_lib import commands_core as _commands_core
from cfc_lib import loop as _loop
from cfc_lib import review_workflow as _review_workflow
from cfc_lib import tmux_ops as _tmux_ops


def _sync_compat_hooks() -> None:
    """Propagate legacy ``scripts/cfc.py`` monkeypatches into split modules."""
    _tmux_ops.tmux_send = globals()["tmux_send"]
    _tmux_ops.tmux_capture = globals()["tmux_capture"]
    _tmux_ops.wait_for_tmux_verdict = globals()["wait_for_tmux_verdict"]
    _tmux_ops.ensure_gjc_tmux_session = globals()["ensure_gjc_tmux_session"]
    _tmux_ops.ensure_isolated_tmux_targets = globals()["ensure_isolated_tmux_targets"]
    _tmux_ops.tmux_kill_session = globals()["tmux_kill_session"]

    _loop.tmux_capture = globals()["tmux_capture"]
    _loop.wait_for_tmux_verdict = globals()["wait_for_tmux_verdict"]
    _loop.send_tmux_prompt = globals()["send_tmux_prompt"]
    _loop.ensure_isolated_tmux_targets = globals()["ensure_isolated_tmux_targets"]
    _loop.cleanup_isolated_tmux_sessions = globals()["cleanup_isolated_tmux_sessions"]

    _commands_core.send_tmux_prompt = globals()["send_tmux_prompt"]
    _commands_core.cleanup_isolated_tmux_sessions = globals()["cleanup_isolated_tmux_sessions"]
    _review_workflow.send_tmux_prompt = globals()["send_tmux_prompt"]


def cmd_capture(args):
    _sync_compat_hooks()
    return _loop.cmd_capture(args)


def cmd_loop(args):
    _sync_compat_hooks()
    return _loop.cmd_loop(args)


def cmd_gjc(args):
    _sync_compat_hooks()
    return _loop.cmd_gjc(args)


def cmd_review(args):
    _sync_compat_hooks()
    return _commands_core.cmd_review(args)


def cmd_repair(args):
    _sync_compat_hooks()
    return _review_workflow.cmd_repair(args)


def main(argv=None) -> int:
    _sync_compat_hooks()
    return _cli.main(argv)

if __name__ == "__main__":
    raise SystemExit(main())
