from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .common import now_iso, write_json

RECEIPT_RE = re.compile(r"(?m)^\s*CFC_EVIDENCE_RECORDED:\s*(.+?)\s*$")
ARTIFACT_PATTERNS = [
    "EXECUTION.iteration-*.md",
    "EXECUTION.iteration-*.fallback-*.md",
    "GJC_LOG*.md",
    "REPAIR_RESULT.iteration-*.md",
    "REVIEW.iteration-*.md",
]


def evidence_required(run: dict[str, Any]) -> bool:
    evidence = run.get("evidence", {}) or {}
    return bool(evidence.get("require_receipts") or evidence.get("require_receipt"))


def _inside(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _resolve_receipt(root: Path, rd: Path, raw: str) -> Path:
    text = raw.strip().strip("`").strip()
    path = Path(text).expanduser()
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.extend([root / path, rd / path, rd / "evidence" / path])
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def validate_receipt(root: Path, rd: Path, raw: str) -> dict[str, Any]:
    target = _resolve_receipt(root, rd, raw)
    evidence_dir = rd / "evidence"
    result: dict[str, Any] = {
        "raw": raw.strip(),
        "path": str(target),
        "valid": False,
        "exists": False,
        "non_empty": False,
        "inside_evidence_dir": False,
        "symlink": False,
        "sha256": None,
        "blockers": [],
    }
    result["inside_evidence_dir"] = _inside(evidence_dir, target)
    if not result["inside_evidence_dir"]:
        result["blockers"].append("receipt path is outside the run evidence directory")
        return result
    if target.exists() and target.is_symlink():
        result["symlink"] = True
        result["blockers"].append("receipt path must not be a symlink")
        return result
    if not target.exists() or not target.is_file():
        result["blockers"].append("receipt file is missing")
        return result
    result["exists"] = True
    data = target.read_bytes()
    if not data:
        result["blockers"].append("receipt file is empty")
        return result
    result["non_empty"] = True
    result["sha256"] = hashlib.sha256(data).hexdigest()
    result["valid"] = True
    return result


def scan_evidence_receipts(root: Path, run: dict[str, Any], rd: Path) -> dict[str, Any]:
    receipts: list[dict[str, Any]] = []
    scanned_artifacts: list[str] = []
    for pattern in ARTIFACT_PATTERNS:
        for artifact in sorted(rd.glob(pattern)):
            if not artifact.is_file():
                continue
            scanned_artifacts.append(artifact.name)
            text = artifact.read_text(encoding="utf-8", errors="ignore")
            for match in RECEIPT_RE.finditer(text):
                item = validate_receipt(root, rd, match.group(1))
                item["source_artifact"] = artifact.name
                receipts.append(item)
    blockers = [
        f"{item['source_artifact']}: {blocker}: {item['raw']}"
        for item in receipts
        for blocker in item.get("blockers", [])
    ]
    required = evidence_required(run)
    if required and not receipts:
        blockers.append("evidence receipt is required but no CFC_EVIDENCE_RECORDED line was found")
    status = "PASS" if receipts and not blockers else "FAIL" if blockers else "MISSING"
    return {
        "version": 1,
        "run_id": run.get("id"),
        "generated_at": now_iso(),
        "required": required,
        "status": status,
        "scanned_artifacts": scanned_artifacts,
        "receipts": receipts,
        "blockers": blockers,
    }


def write_evidence_receipts(root: Path, run: dict[str, Any], rd: Path) -> dict[str, Any]:
    report = scan_evidence_receipts(root, run, rd)
    artifact = rd / "EVIDENCE_RECEIPTS.json"
    write_json(artifact, report)
    evidence = dict(run.get("evidence", {}) or {})
    evidence.update({
        "artifact": str(artifact),
        "status": report["status"],
        "required": report["required"],
        "receipt_count": len(report["receipts"]),
        "blockers": report["blockers"],
        "updated_at": report["generated_at"],
    })
    run["evidence"] = evidence
    return report
