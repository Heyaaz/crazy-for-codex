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

from .common import deep_merge, read_json
from .constants import TRACKED_CONFIG_FILE
from .paths import cfc_path

def load_config(root: Path) -> dict[str, Any]:
    config: dict[str, Any] = {}
    local = cfc_path(root) / "config.json"
    if local.exists():
        config = deep_merge(config, read_json(local, default={}))
    tracked = root / TRACKED_CONFIG_FILE
    if tracked.exists():
        # Tracked repo config overrides generated `.cfc/config.json` defaults.
        config = deep_merge(config, read_json(tracked, default={}))
    local_override = cfc_path(root) / "config.local.json"
    if local_override.exists():
        # Optional ignored local override for private machine-specific commands.
        config = deep_merge(config, read_json(local_override, default={}))
    return config

def adapter_config(config: dict[str, Any]) -> dict[str, Any]:
    adapters = config.get("adapters") or config.get("adapter") or {}
    return adapters if isinstance(adapters, dict) else {}

def adapter_profiles(config: dict[str, Any]) -> dict[str, Any]:
    profiles = adapter_config(config).get("profiles") or {}
    return profiles if isinstance(profiles, dict) else {}

def configured_profile_command(config: dict[str, Any], profile: str | None) -> str | None:
    if not profile:
        return None
    profiles = adapter_profiles(config)
    value = profiles.get(profile)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        command = value.get("command")
        return str(command) if command else None
    return None

def configured_reviewer_command(config: dict[str, Any], profile: str | None = None) -> str | None:
    adapters = adapter_config(config)
    profile_name = profile or adapters.get("reviewer_profile") or adapters.get("reviewerProfile") or "codex"
    return configured_profile_command(config, str(profile_name)) or configured_profile_command(config, "codex-reviewer")

def request_looks_complex(request: str, config: dict[str, Any]) -> bool:
    adapters = adapter_config(config)
    auto = adapters.get("auto") if isinstance(adapters.get("auto"), dict) else {}
    keywords = auto.get("complex_keywords") or auto.get("complexKeywords") or [
        "architecture", "architect", "multi-file", "refactor", "migration", "security", "auth",
        "async", "tmux", "state", "concurrency", "race", "database", "schema", "protocol",
        "아키텍처", "여러 파일", "리팩터", "마이그레이션", "보안", "인증", "비동기", "상태", "동시성", "복잡",
    ]
    lowered = request.lower()
    return any(str(k).lower() in lowered for k in keywords)

def select_executor_profile(request: str, config: dict[str, Any], explicit_profile: str | None = None) -> str | None:
    adapters = adapter_config(config)
    profile = explicit_profile or adapters.get("executor_profile") or adapters.get("executorProfile")
    if not profile:
        return None
    profile = str(profile)
    if profile != "auto":
        return profile
    auto = adapters.get("auto") if isinstance(adapters.get("auto"), dict) else {}
    complex_profile = str(auto.get("complex_executor_profile") or auto.get("complexExecutorProfile") or "complex")
    default_profile = str(auto.get("default_executor_profile") or auto.get("defaultExecutorProfile") or "cheap")
    return complex_profile if request_looks_complex(request, config) else default_profile

def configured_executor_command(config: dict[str, Any], request: str, profile: str | None = None) -> tuple[str | None, str | None]:
    selected = select_executor_profile(request, config, profile)
    return configured_profile_command(config, selected), selected

def apply_configured_adapters(args: argparse.Namespace, root: Path) -> None:
    if getattr(args, "send", False):
        return
    config = load_config(root)
    if not getattr(args, "executor_command", None):
        command, profile = configured_executor_command(config, getattr(args, "request", ""), getattr(args, "executor_profile", None))
        if command:
            args.executor_command = command
            args.executor_profile = profile
    if not getattr(args, "reviewer_command", None):
        command = configured_reviewer_command(config, getattr(args, "reviewer_profile", None))
        if command:
            args.reviewer_command = command
