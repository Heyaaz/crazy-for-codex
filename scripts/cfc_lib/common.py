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

from .constants import FALSE_STRINGS, TRUE_STRINGS

_MISSING = object()

def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    if value in TRUE_STRINGS:
        return True
    if value in FALSE_STRINGS:
        return False
    return default

def first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None

def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")

def slugify(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9가-힣._-]+", "-", text.strip()).strip("-").lower()
    return s[:80] or "task"

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def read_json(path: Path, default: Any = _MISSING) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not _MISSING:
            return default
        raise

def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def append_ledger(run_dir: Path, phase: str, status: str, **data: Any) -> None:
    event = {"ts": now_iso(), "phase": phase, "status": status, **data}
    path = run_dir / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def run_cmd(cmd: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)

def shell_cmd(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)

def match_any(path: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if pat == "*":
            return True
        if fnmatch.fnmatch(path, pat) or path == pat or path.startswith(pat.rstrip("/") + "/"):
            return True
    return False
