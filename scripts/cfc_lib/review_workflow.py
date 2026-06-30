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
import tempfile
from pathlib import Path
from typing import Any

from .common import append_ledger, env_bool, now_iso, write_json
from .gjc_rpc import command_looks_like_gjc_rpc, run_gjc_rpc_command
from .learn import run_learn
from .paths import root_path
from .prompts import render_blockers_md, render_repair_prompt
from .review_result import latest_review_result, parse_review_result
from .state import active_run
from .tmux_ops import send_tmux_prompt

def run_agent_command(command: str, prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    if command_looks_like_gjc_rpc(command):
        return run_gjc_rpc_command(command, prompt, cwd, timeout)
    prompt_file: Path | None = None
    stdin = prompt
    if "{prompt_file}" in command:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="cfc-agent-prompt-", suffix=".md", delete=False) as f:
            f.write(prompt)
            prompt_file = Path(f.name)
        command = command.replace("{prompt_file}", shlex.quote(str(prompt_file)))
        stdin = None
    try:
        return subprocess.run(command, cwd=str(cwd), input=stdin, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        timeout_msg = f"command timed out after {timeout} seconds"
        stderr = (stderr + "\n" if stderr else "") + timeout_msg
        return subprocess.CompletedProcess(command, 124, stdout, stderr)
    finally:
        if prompt_file:
            try:
                prompt_file.unlink()
            except FileNotFoundError:
                pass

def classify_review_file(root: Path, run: dict[str, Any], rd: Path, path: Path) -> dict[str, Any]:
    parsed = parse_review_result(path.read_text(encoding="utf-8", errors="ignore"))
    (rd / "BLOCKERS.md").write_text(render_blockers_md(path, parsed), encoding="utf-8")
    run["review"] = {"verdict": parsed["verdict"], "blockers": parsed["blockers"], "review_file": str(path), "classified_at": now_iso()}
    if run.get("awaiting", {}).get("phase") == "reviewer":
        run.pop("awaiting", None)
    write_json(rd / "RUN.json", run)
    append_ledger(rd, "review_classify", parsed["verdict"].lower(), blocker_count=len(parsed["blockers"]), review_file=str(path))
    auto_apply_high = env_bool("CFC_REVIEW_AUTO_APPLY_HIGH_LEARN", False) and parsed["verdict"] == "PASS"
    learn_md, candidates, applied = run_learn(root, run, rd, auto_apply_high=auto_apply_high)
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
        next_iteration = args.iteration + 1
        send_tmux_prompt(run, rd, "repair_send", target, prompt, iteration=args.iteration, next_iteration=next_iteration, prompt=str(path))
        run["awaiting"] = {"phase": "executor", "iteration": next_iteration, "target": target, "prompt": str(path), "since": now_iso(), "source_review_iteration": args.iteration}
        write_json(rd / "RUN.json", run)
        print(f"Sent repair prompt to tmux target: {target}")
