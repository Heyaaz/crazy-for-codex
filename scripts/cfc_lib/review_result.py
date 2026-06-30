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


def extract_review_result_name(iteration: int) -> str:
    return f"REVIEW.iteration-{iteration}.md"

def parse_review_result(text: str) -> dict[str, Any]:
    if not text.strip():
        return {"verdict": "REVIEW_BLOCKED", "blockers": ["review produced no output"], "major": [], "minor": []}
    verdict_match = re.search(r"(?im)^\s*Verdict\s*:\s*([A-Z_]+)", text)
    verdict = verdict_match.group(1).upper() if verdict_match else "REVIEW_BLOCKED"

    def section_items(name: str) -> list[str]:
        m = re.search(rf"(?ims)^##\s*{re.escape(name)}\s*$\n(.*?)(?=^##\s+|\Z)", text)
        if not m:
            # Also accept `BLOCKERS:` style.
            m2 = re.search(rf"(?im)^\s*{re.escape(name)}\s*:\s*(.+)$", text)
            if not m2:
                return []
            raw = m2.group(1).strip()
            return [] if raw.lower() in {"none", "(none)", "없음", "n/a"} else [raw]
        items = []
        for line in m.group(1).splitlines():
            stripped = line.strip()
            if not stripped or stripped.lower() in {"- none", "none", "- (none)", "(none)", "- 없음", "없음"}:
                continue
            if stripped.startswith("-"):
                items.append(stripped.lstrip("- ").strip())
            elif stripped:
                items.append(stripped)
        return items

    blockers = section_items("BLOCKERS") or section_items("BLOCKER")
    major = section_items("MAJOR")
    minor = section_items("MINOR")
    if not verdict_match:
        blockers = ["review missing required final Verdict line"] + blockers
    elif verdict not in {"PASS", "REVIEW_BLOCKED"}:
        blockers = [f"review returned unsupported Verdict: {verdict}; expected PASS or REVIEW_BLOCKED"] + blockers
    elif verdict == "REVIEW_BLOCKED" and not blockers:
        blockers = ["review returned REVIEW_BLOCKED without parsed BLOCKERS"]
    blocked = verdict == "REVIEW_BLOCKED" or bool(blockers)
    return {"verdict": "REVIEW_BLOCKED" if blocked else "PASS", "blockers": blockers, "major": major, "minor": minor}

def is_review_infrastructure_blocker(blockers: list[str]) -> bool:
    text = "\n".join(blockers).lower()
    return any(
        phrase in text
        for phrase in [
            "reviewer did not complete",
            "timed out waiting for final verdict",
            "review evidence is incomplete",
            "review missing required final verdict",
            "review produced no output",
            "review returned unsupported verdict",
            "review returned review_blocked without parsed blockers",
        ]
    )

def latest_review_result(rd: Path) -> tuple[Path | None, dict[str, Any]]:
    files = [p for p in sorted(rd.glob("REVIEW.iteration-*.md")) if p.is_file()]
    if not files:
        return None, {"verdict": "PASS", "blockers": [], "major": [], "minor": []}
    path = files[-1]
    return path, parse_review_result(path.read_text(encoding="utf-8", errors="ignore"))
