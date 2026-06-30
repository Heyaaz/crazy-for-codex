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

from .commands_core import cmd_check, cmd_diff, cmd_done, cmd_init, cmd_start
from .common import append_ledger, env_bool, now_iso, sha256_text, write_json
from .config import adapter_config, apply_configured_adapters, configured_executor_command, configured_executor_fallbacks, configured_reviewer_command, load_config
from .git_ops import is_git_repo, nearest_git_root
from .learn import cmd_learn
from .paths import cfc_path, root_path
from .prompts import build_prompt, render_blockers_md, render_repair_prompt, render_review_prompt
from .review_result import extract_review_result_name, is_review_infrastructure_blocker, parse_review_result
from .review_workflow import classify_review_file, run_agent_command
from .runtime_env import enforce_external_terminal_for_live_adapters
from .state import active_run
from .tmux_ops import ensure_isolated_tmux_targets, render_reviewer_timeout_result, send_tmux_prompt, tmux_capture, wait_for_tmux_verdict

def next_available_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}.{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise SystemExit(f"Refusing to overwrite existing artifact: {path}")

def executor_command_attempts(args: argparse.Namespace) -> list[dict[str, Any]]:
    attempts: list[dict[str, Any]] = []
    primary_command = getattr(args, "executor_command", None)
    if primary_command:
        attempts.append({
            "profile": getattr(args, "executor_profile", None),
            "command": primary_command,
            "fallback": False,
            "fallback_index": 0,
        })
    seen_commands = {primary_command} if primary_command else set()
    for fallback_index, fallback in enumerate(getattr(args, "executor_fallbacks", []) or [], start=1):
        profile: str | None = None
        command: str | None = None
        if isinstance(fallback, dict):
            profile = str(fallback["profile"]) if fallback.get("profile") else None
            command = str(fallback["command"]) if fallback.get("command") else None
        elif isinstance(fallback, (list, tuple)) and len(fallback) >= 2:
            profile = str(fallback[0]) if fallback[0] else None
            command = str(fallback[1]) if fallback[1] else None
        elif isinstance(fallback, str):
            command = fallback
        if not command or command in seen_commands:
            continue
        seen_commands.add(command)
        attempts.append({
            "profile": profile,
            "command": command,
            "fallback": True,
            "fallback_index": fallback_index,
        })
    return attempts

def execution_result_path(rd: Path, iteration: int, attempt: dict[str, Any]) -> Path:
    if attempt.get("fallback"):
        return next_available_path(rd / f"EXECUTION.iteration-{iteration}.fallback-{attempt.get('fallback_index')}.md")
    return next_available_path(rd / f"EXECUTION.iteration-{iteration}.md")

def write_execution_result(path: Path, attempt: dict[str, Any], res: subprocess.CompletedProcess[str]) -> None:
    profile = attempt.get("profile") or "custom"
    fallback = "yes" if attempt.get("fallback") else "no"
    path.write_text(
        f"# Execution Result\n\n"
        f"Profile: `{profile}`\n"
        f"Fallback: `{fallback}`\n"
        f"Command: `{attempt['command']}`\n"
        f"Exit: `{res.returncode}`\n\n"
        f"## stdout\n```text\n{res.stdout}\n```\n\n"
        f"## stderr\n```text\n{res.stderr}\n```\n",
        encoding="utf-8",
    )

def run_executor_command_attempts(args: argparse.Namespace, prompt: str, root: Path, rd: Path, run: dict[str, Any], iteration: int) -> None:
    attempts = executor_command_attempts(args)
    if not attempts:
        raise SystemExit("cfc loop requires an executor adapter: pass --executor-command or use --send with --executor-target")
    failures: list[dict[str, Any]] = []
    for attempt_number, attempt in enumerate(attempts, start=1):
        command = attempt["command"]
        res = run_agent_command(command, prompt, root, args.timeout)
        out = execution_result_path(rd, iteration, attempt)
        write_execution_result(out, attempt, res)
        append_ledger(
            rd,
            "execute_command",
            "pass" if res.returncode == 0 else "fail",
            iteration=iteration,
            attempt=attempt_number,
            profile=attempt.get("profile"),
            fallback=bool(attempt.get("fallback")),
            fallback_index=attempt.get("fallback_index"),
            exit_code=res.returncode,
            path=str(out),
        )
        if res.returncode == 0:
            run.setdefault("runner", {})["executor_last_success"] = {
                "profile": attempt.get("profile"),
                "command": command,
                "fallback": bool(attempt.get("fallback")),
                "attempt": attempt_number,
                "artifact": str(out),
            }
            if attempt.get("fallback"):
                run["runner"]["executor_fallback_used"] = run["runner"]["executor_last_success"]
            write_json(rd / "RUN.json", run)
            return
        failures.append({
            "profile": attempt.get("profile"),
            "command": command,
            "fallback": bool(attempt.get("fallback")),
            "attempt": attempt_number,
            "exit_code": res.returncode,
            "artifact": str(out),
        })
    run["status"] = "execute_failed"
    run.setdefault("runner", {})["executor_failures"] = failures
    write_json(rd / "RUN.json", run)
    raise SystemExit(failures[-1]["exit_code"] if failures else 1)

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
        send_tmux_prompt(run, rd, "gjc_send", target, prompt, prompt_path=str(p))
        print(f"Sent prompt to tmux target: {target}")

def continue_after_executor_capture(root: Path, run: dict[str, Any], rd: Path, iteration: int) -> None:
    """After an async executor finishes, run checks and dispatch independent review."""
    run.pop("awaiting", None)
    write_json(rd / "RUN.json", run)
    cmd_diff(argparse.Namespace(root=str(root)))
    cmd_check(argparse.Namespace(root=str(root)))
    run, rd = active_run(root)
    check = run.get("check", {}) or {}
    if check.get("verdict") == "FAIL" and run.get("loop", {}).get("review_on_check_fail") is False:
        blockers = check.get("failures") or ["check failed and review_on_check_fail is disabled"]
        parsed = {"verdict": "REVIEW_BLOCKED", "blockers": blockers, "major": [], "minor": []}
        (rd / "BLOCKERS.md").write_text(render_blockers_md(None, parsed), encoding="utf-8")
        run["status"] = "review_blocked"
        run["review"] = {"verdict": "REVIEW_BLOCKED", "blockers": blockers, "review_file": None, "classified_at": now_iso()}
        run.pop("awaiting", None)
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "async_loop", "check_failed_no_review", iteration=iteration, blocker_count=len(blockers))
        print("CfC check failed and review_on_check_fail is disabled. Inspect BLOCKERS.md.")
        return
    review_prompt = render_review_prompt(run, root, rd, iteration)
    review_prompt_path = rd / f"REVIEW_PROMPT.iteration-{iteration}.md"
    review_prompt_path.write_text(review_prompt, encoding="utf-8")
    append_ledger(rd, "review_prompt", "done", iteration=iteration, path=str(review_prompt_path))
    reviewer_target = run.get("runner", {}).get("reviewer_target") or os.environ.get("CFC_REVIEWER_TARGET")
    if reviewer_target:
        send_tmux_prompt(run, rd, "review_send", reviewer_target, review_prompt, iteration=iteration)
        run["awaiting"] = {"phase": "reviewer", "iteration": iteration, "target": reviewer_target, "prompt": str(review_prompt_path), "since": now_iso()}
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "async_loop", "waiting_for_reviewer", iteration=iteration, target=reviewer_target)
        print(f"CfC dispatched reviewer prompt to {reviewer_target} after executor capture.")
    else:
        # No external reviewer target is configured, so the review prompt was
        # only written to disk. Preserve an awaiting-reviewer state (with no
        # target) so cfc done refuses until the operator classifies the review,
        # instead of leaving the run looking like nothing is pending.
        run["awaiting"] = {"phase": "reviewer", "iteration": iteration, "target": None, "prompt": str(review_prompt_path), "since": now_iso(), "manual": True}
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "async_loop", "review_prompt_ready", iteration=iteration, path=str(review_prompt_path))
        print(f"Wrote reviewer prompt (no reviewer target configured; classify manually): {review_prompt_path}")

def continue_after_review_classification(root: Path, run: dict[str, Any], rd: Path, iteration: int, parsed: dict[str, Any]) -> None:
    """After async review, either stop on pass or send BLOCKERS back to executor for repair."""
    blockers = parsed.get("blockers", [])
    if parsed.get("verdict") != "REVIEW_BLOCKED" and not blockers:
        append_ledger(rd, "async_loop", "review_pass", iteration=iteration)
        print("CfC review passed. Run `cfc done --root ...` to finalize.")
        return
    if is_review_infrastructure_blocker(blockers):
        run["status"] = "review_blocked"
        run.pop("awaiting", None)
        write_json(rd / "RUN.json", run)
        append_ledger(rd, "async_loop", "review_incomplete", iteration=iteration, blocker_count=len(blockers))
        print("CfC review did not complete cleanly. Inspect REVIEW.iteration-*.md and rerun review after fixing reviewer scope/timeout.")
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
        next_iteration = iteration + 1
        send_tmux_prompt(run, rd, "repair_send", executor_target, repair_prompt, iteration=iteration, next_iteration=next_iteration, prompt=str(repair_path))
        run["awaiting"] = {"phase": "executor", "iteration": next_iteration, "target": executor_target, "prompt": str(repair_path), "since": now_iso(), "source_review_iteration": iteration}
        write_json(rd / "RUN.json", run)
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
        if not args.timeout_seconds:
            # timeout-seconds 0 means wait indefinitely for a final Verdict line.
            # Flag this explicitly so the operator knows the capture will not
            # time out and must be interrupted manually if the reviewer hangs.
            append_ledger(rd, "capture_wait", "infinite_wait", target=target, timeout_seconds=0)
            print(f"cfc capture: --timeout-seconds 0 means waiting indefinitely for a final Verdict from {target}; interrupt manually if the reviewer hangs.")
        append_ledger(rd, "capture_wait", "start", target=target, timeout_seconds=args.timeout_seconds)
        try:
            text = wait_for_tmux_verdict(target, args.lines, poll_seconds=args.poll_seconds, timeout_seconds=args.timeout_seconds)
        except TimeoutError as e:
            append_ledger(rd, "capture_wait", "timeout", target=target, timeout_seconds=args.timeout_seconds)
            if not is_awaiting_reviewer:
                raise SystemExit(str(e))
            cap = tmux_capture(target, args.lines)
            captured_text = cap.stdout if cap.returncode == 0 else cap.stderr
            text = render_reviewer_timeout_result(target, args.timeout_seconds, captured_text)
    else:
        p = tmux_capture(target, args.lines)
        if p.returncode != 0:
            append_ledger(rd, "capture", "fail", target=target, error=p.stderr.strip())
            raise SystemExit(p.stderr.strip())
        text = p.stdout
    capture_iteration = int(awaiting.get("iteration") or args.iteration or 1)
    if is_awaiting_executor and not args.tmux_target:
        out = next_available_path(rd / f"EXECUTION.iteration-{capture_iteration}.md")
        out.write_text("# Execution Result\n\nCommand: `tmux capture`\nExit: `0`\n\n## stdout\n```text\n" + text + "\n```\n\n## stderr\n```text\n\n```\n", encoding="utf-8")
    else:
        out = next_available_path(rd / f"GJC_LOG.{dt.datetime.now().strftime('%H%M%S')}.md")
        out.write_text("# GJC Captured Log\n\n```text\n" + text + "\n```\n", encoding="utf-8")
    append_ledger(rd, "capture", "done", target=target, path=str(out), waited_for_verdict=wait_for_verdict)
    print(f"Captured tmux log: {out}")
    if wait_for_verdict and is_awaiting_reviewer:
        iteration = int(awaiting.get("iteration") or args.iteration or 1)
        review_path = next_available_path(rd / extract_review_result_name(iteration))
        review_path.write_text(text, encoding="utf-8")
        parsed = classify_review_file(root, run, rd, review_path)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
        run_after, rd_after = active_run(root)
        continue_after_review_classification(root, run_after, rd_after, iteration, parsed)
    elif is_awaiting_executor and not args.tmux_target:
        iteration = int(awaiting.get("iteration") or args.iteration or 1)
        continue_after_executor_capture(root, run, rd, iteration)

def cmd_loop(args: argparse.Namespace) -> None:
    root = root_path(args)
    apply_configured_adapters(args, root)
    if not args.executor_command and not args.send:
        raise SystemExit("cfc loop requires an executor adapter: pass --executor-command or use --send with --executor-target")
    if not args.reviewer_command and not args.send:
        raise SystemExit("cfc loop requires an independent reviewer: pass --reviewer-command or use --send with --reviewer-target")
    enforce_external_terminal_for_live_adapters(args, root)
    if not cfc_path(root).exists():
        cmd_init(argparse.Namespace(root=str(root)))
    start_args = argparse.Namespace(
        root=str(root), title=args.request, allow=args.allow, forbid=args.forbid, verify=args.verify,
        tmux_target=args.executor_target, allow_dirty=args.allow_dirty, replace=args.replace,
        max_iterations=args.max_iterations,
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
    elif args.executor_command:
        run.setdefault("runner", {})["executor_profile"] = getattr(args, "executor_profile", None)
        run["runner"]["executor_command"] = args.executor_command
        run["runner"]["executor_fallbacks"] = getattr(args, "executor_fallbacks", []) or []
        run["runner"]["reviewer_profile"] = getattr(args, "reviewer_profile", None)
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
            run_executor_command_attempts(args, prompt, root, rd, run, iteration)
        elif args.send:
            send_tmux_prompt(run, rd, "execute_send", args.executor_target, prompt, iteration=iteration)
            if not args.tmux_wait_seconds:
                run["awaiting"] = {"phase": "executor", "iteration": iteration, "target": args.executor_target, "prompt": str(prompt_path), "since": now_iso()}
                write_json(rd / "RUN.json", run)
                append_ledger(rd, "loop", "waiting_for_executor", iteration=iteration, target=args.executor_target)
                print(f"CfC dispatched executor prompt to {args.executor_target} and is waiting for external completion before check/review.")
                print(f"Run dir: {rd}")
                return
            subprocess.run(["sleep", str(args.tmux_wait_seconds)], check=False)
            cap = subprocess.run(["tmux", "capture-pane", "-t", args.executor_target, "-p", "-S", f"-{args.capture_lines}"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (rd / f"EXECUTION.iteration-{iteration}.md").write_text("# Execution Result\n\nCommand: `tmux capture`\nExit: `" + str(cap.returncode) + "`\n\n## stdout\n```text\n" + cap.stdout + "\n```\n\n## stderr\n```text\n" + cap.stderr + "\n```\n", encoding="utf-8")
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
                send_tmux_prompt(run, rd, "review_send", args.reviewer_target, review_prompt, iteration=iteration)
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
        if blockers and is_review_infrastructure_blocker(blockers):
            run["status"] = "review_blocked"
            write_json(rd / "RUN.json", run)
            append_ledger(rd, "loop", "review_incomplete", iteration=iteration, blocker_count=len(blockers))
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
    happy = run.get("status") != "review_blocked" and run.get("check", {}).get("verdict") != "FAIL" and not final_parsed.get("blockers")
    if happy:
        # cmd_done already runs run_learn (and honors --apply-learn). Do not
        # run cmd_learn separately here: that would double-write LEARN.md and
        # double-append the wiki log for every successful loop.
        cmd_done(argparse.Namespace(root=str(root), force=False, apply_learn=args.apply_learn))
    else:
        cmd_learn(argparse.Namespace(root=str(root), apply=args.apply_learn))
        print("CfC loop ended review_blocked/failed. Inspect BLOCKERS.md and run artifacts.")

def default_loop_namespace(request: str, root: str = ".", replace: bool = False, allow_dirty: bool = False) -> argparse.Namespace:
    root_path_value = nearest_git_root(Path(root))
    config = load_config(root_path_value if is_git_repo(root_path_value) else Path(root))
    adapters = adapter_config(config)
    mode = str(adapters.get("mode") or "tmux")
    executor_command, executor_profile = configured_executor_command(config, request)
    executor_fallbacks = configured_executor_fallbacks(config, executor_profile)
    reviewer_command = configured_reviewer_command(config)
    command_mode = mode == "command" or bool(executor_command or reviewer_command)
    return argparse.Namespace(
        root=root,
        request=request,
        allow=["*"],
        forbid=None,
        verify=config.get("verification", {}).get("commands") or ["git diff --check"],
        max_iterations=int(config.get("loop", {}).get("max_iterations") or os.environ.get("CFC_MAX_ITERATIONS", "3")),
        executor_target=str(adapters.get("executor_target") or adapters.get("executorTarget") or os.environ.get("CFC_EXECUTOR_TARGET", "gjc:0.0")),
        reviewer_target=str(adapters.get("reviewer_target") or adapters.get("reviewerTarget") or os.environ.get("CFC_REVIEWER_TARGET", "cfc-review:0.0")),
        send=False if command_mode else env_bool("CFC_SEND", True),
        tmux_wait_seconds=int(adapters.get("tmux_wait_seconds") or adapters.get("tmuxWaitSeconds") or os.environ.get("CFC_TMUX_WAIT_SECONDS", "0")),
        capture_lines=int(adapters.get("capture_lines") or adapters.get("captureLines") or os.environ.get("CFC_CAPTURE_LINES", "5000")),
        isolated_tmux=bool(adapters.get("isolated_tmux", adapters.get("isolatedTmux", env_bool("CFC_ISOLATED_TMUX", True)))),
        executor_command=executor_command or os.environ.get("CFC_EXECUTOR_COMMAND") or None,
        executor_fallbacks=executor_fallbacks,
        reviewer_command=reviewer_command or os.environ.get("CFC_REVIEWER_COMMAND") or None,
        executor_profile=executor_profile,
        reviewer_profile=adapters.get("reviewer_profile") or adapters.get("reviewerProfile") or "codex",
        timeout=int(adapters.get("timeout") or os.environ.get("CFC_TIMEOUT", "600")),
        allow_dirty=allow_dirty,
        replace=replace,
        apply_learn=env_bool("CFC_APPLY_LEARN", False),
        review_on_check_fail=env_bool("CFC_REVIEW_ON_CHECK_FAIL", True),
    )
