from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .ambient import apply_ambient_global_learn, build_ambient_global_context
from .common import read_json
from .evidence import RECEIPT_RE, validate_receipt
from .git_ops import git_changed_files
from .loop import default_loop_namespace
from .paths import current_file, root_path, runs_dir
from .runtime_env import external_terminal_handoff_payload
from .state import current_active_run_or_none


CFC_KEYWORD_RE = re.compile(
    r"(?ix)"
    r"(^|[\s`$])("
    r"\$?cfc\b|cfc\s*로|cfc\s*써서|cfc\s*돌려|cfc\s*진행|"
    r"cfc\s+loop|gjc\s+loop|executor\s*/\s*reviewer"
    r")"
)


def _read_stdin() -> str:
    try:
        return sys.stdin.read()
    except Exception:
        return ""


def _json_or_text(raw: str) -> tuple[dict[str, Any], str]:
    text = raw.strip()
    if not text:
        return {}, ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}, raw
    if isinstance(value, dict):
        prompt = value.get("prompt") or value.get("message") or value.get("user_prompt") or value.get("text") or ""
        return value, str(prompt)
    return {}, raw


def _active(root: Path) -> tuple[dict[str, Any], Path] | None:
    try:
        return current_active_run_or_none(root)
    except SystemExit:
        return None


def _handoff_for_prompt(root: Path, prompt: str) -> dict[str, Any]:
    ns = default_loop_namespace(prompt, root=str(root))
    return external_terminal_handoff_payload(root, ns)


def render_router_contract(root: Path, prompt: str, handoff: dict[str, Any] | None = None) -> str:
    handoff_block = ""
    if handoff and handoff.get("handoff_required"):
        attempts = "\n".join(
            f"  - {item['phase']} ({item['profile']}): {item['command']}"
            for item in handoff.get("live_adapter_attempts", [])
        )
        handoff_block = f"""

Codex App external-terminal handoff:
- This session is inside CODEX_SANDBOX and the selected CFC adapters are live commands.
- Do not run `cfc plugin run` directly inside the Codex App sandbox.
- Run or present this command from an external terminal/tmux pane instead:
  {handoff.get('external_command')}
- To inspect the handoff without starting a run, use:
  cfc plugin run {json.dumps(prompt, ensure_ascii=False)} --root {root} --handoff-only
- Detected live adapters:
{attempts or '  - (none)'}
"""
    return f"""<cfc-router-mode>
This user request is explicitly routed to CfC.

Hard routing contract:
- Treat the user's `cfc` / `$cfc` prefix as an execution directive, not as a normal skill hint.
- Do NOT perform the requested repository task directly before CfC accepts or refuses it.
- Before CfC accepts/refuses, do NOT edit files, run git merge/rebase/cherry-pick/checkout, run project tests, run code generators, or manually inspect/modify task files except for reading CfC status/skill/config.
- Your first shell command for the task MUST be `cfc plugin status --root "$PWD"` unless the user supplied an explicit repo path; then use that path.
- If there is no active run, start CfC with `cfc plugin run "<repo-scoped task>" --root <repo>`, unless the external-terminal handoff block below says this Codex App sandbox must not run live adapters directly.
- If an active run exists, do not overwrite it silently. Inspect status/events or cancel/replace only when explicitly requested.
- After CfC returns, verify `.cfc/runs/<id>/RUN.json`, `ledger.jsonl`, `CHECK.md`, `REVIEW.iteration-*.md`, and `DONE.md` or the explicit blocked/refused status.
- You may fall back to direct work ONLY if CfC returns an explicit refusal/blocker that makes direct fallback necessary, and you must say that fallback happened.
- Do not claim CfC controlled the work unless a `cfc plugin run` occurred and run artifacts exist.

Repo root: {root}
{handoff_block}
User request routed to CfC:
{prompt}
</cfc-router-mode>"""


def build_user_prompt_submit_payload(root: Path, raw: str) -> dict[str, Any]:
    _, prompt = _json_or_text(raw)
    active = _active(root)
    explicit = bool(CFC_KEYWORD_RE.search(prompt))
    if explicit:
        handoff = _handoff_for_prompt(root, prompt)
        return {
            "hook": "UserPromptSubmit",
            "mode": "strict",
            "block": False,
            "reason": "explicit_cfc_keyword",
            "injection": render_router_contract(root, prompt, handoff),
            "handoff": handoff,
            "active_run": active[0].get("id") if active else None,
        }
    if active:
        run, rd = active
        return {
            "hook": "UserPromptSubmit",
            "mode": "light",
            "block": False,
            "reason": "active_run_reminder",
            "injection": (
                f"<cfc-active-run>\n"
                f"CfC run `{run.get('id')}` is active at `{rd}`. Keep this run in mind; "
                "do not claim CFC completion until the run is checked, reviewed, and finalized.\n"
                "</cfc-active-run>"
            ),
            "active_run": run.get("id"),
        }
    ambient = build_ambient_global_context(root, prompt)
    if ambient.get("injection"):
        return {
            "hook": "UserPromptSubmit",
            "mode": "light",
            "block": False,
            "reason": "ambient_global_context",
            "injection": ambient["injection"],
            "active_run": None,
            "ambient_context": {
                "item_count": len(ambient.get("items") or []),
                "items": ambient.get("items") or [],
                "budget": ambient.get("budget") or {},
            },
        }
    return {"hook": "UserPromptSubmit", "mode": "light", "block": False, "reason": "no_cfc_keyword", "injection": "", "active_run": None}


def stop_guard_payload(root: Path, raw: str = "") -> dict[str, Any]:
    active = _active(root)
    if not active:
        ambient = apply_ambient_global_learn(root, raw)
        return {
            "hook": "Stop",
            "mode": "light",
            "block": False,
            "reason": "no_active_run",
            "blockers": [],
            "ambient_learn": {
                "enabled": ambient.get("enabled", False),
                "candidate_count": ambient.get("candidate_count", 0),
                "applied_count": ambient.get("applied_count", 0),
                "applied": [
                    {"title": item.get("title"), "path": item.get("path"), "scope": item.get("scope")}
                    for item in ambient.get("applied", [])
                ],
            },
        }
    run, rd = active
    blockers: list[str] = []
    if run.get("awaiting"):
        blockers.append(f"run is awaiting external agent completion: {run.get('awaiting')}")
    if not (rd / "CHECK.md").exists():
        blockers.append("CHECK.md is missing; run cfc check before stopping a CFC-controlled task")
    check = run.get("check", {}) or {}
    if check.get("verdict") == "FAIL":
        blockers.append("latest CFC check verdict is FAIL")
    baseline = set(run.get("precheck", {}).get("changed_files", []))
    changed = [path for path in git_changed_files(root) if path not in baseline]
    review = run.get("review", {}) or {}
    if changed:
        if not sorted(rd.glob("REVIEW.iteration-*.md")):
            blockers.append("changed files exist but independent review artifact is missing")
        if not review:
            blockers.append("changed files exist but RUN.json has no classified review")
        elif str(review.get("verdict", "")).upper() != "PASS":
            blockers.append(f"classified review is not PASS: {review.get('verdict')}")
    gate = run.get("quality_gate", {}) or {}
    if gate.get("status") == "FAIL":
        blockers.append("quality gate status is FAIL")
    if not (rd / "DONE.md").exists():
        blockers.append("DONE.md is missing; finalize with cfc done or cancel the active run")
    return {
        "hook": "Stop",
        "mode": "strict",
        "block": bool(blockers),
        "reason": "active_run_guard",
        "run_id": run.get("id"),
        "run_dir": str(rd),
        "blockers": blockers,
    }


def subagent_stop_payload(root: Path, raw: str, strict: bool = False) -> dict[str, Any]:
    active = _active(root)
    if not active:
        return {"hook": "SubagentStop", "mode": "light", "block": False, "reason": "no_active_run", "blockers": []}
    run, rd = active
    evidence = run.get("evidence", {}) or {}
    required = strict or bool(evidence.get("require_receipts") or evidence.get("require_receipt"))
    text = raw
    parsed, parsed_text = _json_or_text(raw)
    if parsed:
        text = "\n".join(str(parsed.get(key) or "") for key in ["output", "message", "text", "transcript", "final_response"])
        if not text.strip():
            text = parsed_text
    receipts = [validate_receipt(root, rd, match.group(1)) for match in RECEIPT_RE.finditer(text)]
    blockers = [
        f"{blocker}: {receipt['raw']}"
        for receipt in receipts
        for blocker in receipt.get("blockers", [])
    ]
    if required and not receipts:
        blockers.append("evidence receipt is required but no CFC_EVIDENCE_RECORDED line was found")
    return {
        "hook": "SubagentStop",
        "mode": "strict" if required else "light",
        "block": bool(blockers),
        "reason": "evidence_receipt_guard" if required else "evidence_receipt_observe",
        "run_id": run.get("id"),
        "run_dir": str(rd),
        "required": required,
        "receipt_count": len(receipts),
        "receipts": receipts,
        "blockers": blockers,
    }


def _emit(payload: dict[str, Any], json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    injection = payload.get("injection")
    if injection:
        print(injection)
        return
    if payload.get("block"):
        print("\n".join(f"- {item}" for item in payload.get("blockers", [])) or "CFC hook blocked")


def _exit_code(payload: dict[str, Any]) -> int:
    return 2 if payload.get("block") else 0


def cmd_hook_user_prompt_submit(args: argparse.Namespace) -> None:
    payload = build_user_prompt_submit_payload(root_path(args), _read_stdin())
    _emit(payload, args.json)


def cmd_hook_stop(args: argparse.Namespace) -> None:
    payload = stop_guard_payload(root_path(args), _read_stdin())
    _emit(payload, args.json)
    raise SystemExit(_exit_code(payload))


def cmd_hook_subagent_stop(args: argparse.Namespace) -> None:
    payload = subagent_stop_payload(root_path(args), _read_stdin(), strict=args.strict)
    _emit(payload, args.json)
    raise SystemExit(_exit_code(payload))
