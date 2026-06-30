from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .common import now_iso, write_json


def _inside(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _artifact_ref(rd: Path, path: str | Path) -> str:
    p = Path(path)
    if p.is_absolute() and _inside(rd, p):
        return str(p.resolve().relative_to(rd.resolve()))
    return str(p)


def _artifact_status(rd: Path, path: str | Path) -> dict[str, Any]:
    ref = _artifact_ref(rd, path)
    target = Path(ref)
    if not target.is_absolute():
        target = rd / target
    status: dict[str, Any] = {
        "path": ref,
        "exists": False,
        "non_empty": False,
        "sha256": None,
        "inside_run_dir": _inside(rd, target),
    }
    if not status["inside_run_dir"] or not target.exists() or not target.is_file():
        return status
    data = target.read_bytes()
    status.update({
        "exists": True,
        "non_empty": bool(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    })
    return status


def _criterion(
    gate: dict[str, Any],
    criterion_id: str,
    title: str,
    *,
    required: bool,
    passed: bool,
    artifacts: list[str | Path] | None = None,
    blocking: bool = True,
    details: dict[str, Any] | None = None,
) -> None:
    artifact_statuses = [_artifact_status(Path(gate["run_dir"]), path) for path in artifacts or []]
    artifact_blockers = [
        f"{criterion_id}: artifact missing or empty: {artifact['path']}"
        for artifact in artifact_statuses
        if required and blocking and (not artifact["exists"] or not artifact["non_empty"] or not artifact["inside_run_dir"])
    ]
    status = "pass" if passed and not artifact_blockers else "fail" if required else "not_required"
    item = {
        "id": criterion_id,
        "title": title,
        "required": required,
        "blocking": blocking,
        "status": status,
        "artifacts": artifact_statuses,
        "details": details or {},
    }
    gate["criteria"].append(item)
    gate["blockers"].extend(artifact_blockers)
    if required and blocking and status != "pass":
        gate["blockers"].append(f"{criterion_id}: {title} did not pass")


def build_quality_gate(
    *,
    root: Path,
    run: dict[str, Any],
    rd: Path,
    changed_files: list[str],
    review_files: list[Path],
    forced: bool = False,
) -> dict[str, Any]:
    check = run.get("check", {}) or {}
    review = run.get("review", {}) or {}
    evidence = run.get("evidence", {}) or {}
    verification_commands = run.get("verification", {}).get("commands", []) or []
    review_artifacts: list[str | Path] = []
    if review.get("review_file"):
        review_artifacts.append(str(review["review_file"]))
    review_artifacts.extend(review_files)

    gate: dict[str, Any] = {
        "version": 1,
        "run_id": run.get("id"),
        "run_dir": str(rd),
        "repo": str(root),
        "generated_at": now_iso(),
        "forced": forced,
        "criteria": [],
        "blockers": [],
        "coverage": {"required": 0, "passed": 0},
        "status": "UNKNOWN",
    }
    _criterion(
        gate,
        "check_artifact",
        "CHECK.md is present and non-empty",
        required=True,
        passed=(rd / "CHECK.md").exists(),
        artifacts=["CHECK.md"],
        details={"check_verdict": check.get("verdict")},
    )
    _criterion(
        gate,
        "scope_and_verification",
        "scope guard and configured verification did not fail",
        required=True,
        passed=check.get("verdict") != "FAIL",
        artifacts=["CHECK.md"],
        details={
            "check_verdict": check.get("verdict"),
            "failures": check.get("failures") or [],
            "warnings": check.get("warnings") or [],
            "verification_commands": verification_commands,
        },
    )
    _criterion(
        gate,
        "independent_review",
        "changed work has classified PASS independent review evidence",
        required=bool(changed_files),
        passed=(not changed_files) or (str(review.get("verdict", "")).upper() == "PASS"),
        artifacts=review_artifacts if changed_files else [],
        details={
            "changed_files": changed_files,
            "review_verdict": review.get("verdict"),
            "review_file": review.get("review_file"),
        },
    )
    _criterion(
        gate,
        "evidence_receipts",
        "executor evidence receipts are valid when required",
        required=bool(evidence.get("require_receipts")) and bool(changed_files),
        passed=(not evidence.get("require_receipts")) or evidence.get("status") == "PASS",
        artifacts=[evidence.get("artifact")] if evidence.get("artifact") else [],
        details={
            "receipt_status": evidence.get("status"),
            "receipt_count": evidence.get("receipt_count", 0),
            "blockers": evidence.get("blockers") or [],
        },
    )
    _criterion(
        gate,
        "learn_artifact",
        "learning pass ran before finalization",
        required=False,
        passed=(rd / "LEARN.md").exists(),
        artifacts=["LEARN.md"] if (rd / "LEARN.md").exists() else [],
        blocking=False,
    )

    required = [item for item in gate["criteria"] if item["required"] and item["blocking"]]
    passed = [item for item in required if item["status"] == "pass"]
    gate["coverage"] = {"required": len(required), "passed": len(passed)}
    if gate["blockers"] and forced:
        gate["status"] = "FORCED"
    else:
        gate["status"] = "FAIL" if gate["blockers"] else "PASS"
    return gate


def write_quality_gate(
    *,
    root: Path,
    run: dict[str, Any],
    rd: Path,
    changed_files: list[str],
    review_files: list[Path],
    forced: bool = False,
) -> dict[str, Any]:
    gate = build_quality_gate(root=root, run=run, rd=rd, changed_files=changed_files, review_files=review_files, forced=forced)
    artifact = rd / "QUALITY_GATE.json"
    write_json(artifact, gate)
    run["quality_gate"] = {
        "status": gate["status"],
        "artifact": str(artifact),
        "coverage": gate["coverage"],
        "blockers": gate["blockers"],
        "generated_at": gate["generated_at"],
    }
    return gate


def require_quality_gate(gate: dict[str, Any]) -> None:
    if gate.get("status") in {"PASS", "FORCED"}:
        return
    blockers = "\n".join(f"- {blocker}" for blocker in gate.get("blockers", [])) or "- unknown quality gate failure"
    raise SystemExit(f"Quality gate failed; refusing to mark CfC run done.\n{blockers}")
