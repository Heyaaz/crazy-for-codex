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

def configured_executor_fallbacks(config: dict[str, Any], profile: str | None) -> list[dict[str, str]]:
    """Return configured executor fallback attempts for a selected profile.

    Fallback entries are intentionally config-driven so public repos can ship a
    deterministic policy such as `glm -> codex-executor` without requiring
    machine-local environment variables. Each entry is normalized to a profile
    plus command pair that the loop can execute after a failed primary attempt.
    """
    if not profile:
        return []
    adapters = adapter_config(config)
    fallback_map = (
        adapters.get("fallbacks")
        or adapters.get("executor_fallbacks")
        or adapters.get("executorFallbacks")
        or {}
    )
    if not isinstance(fallback_map, dict):
        return []
    raw_chain = fallback_map.get(str(profile))
    if raw_chain is None:
        return []
    raw_entries = raw_chain if isinstance(raw_chain, list) else [raw_chain]
    fallbacks: list[dict[str, str]] = []
    seen: set[tuple[str | None, str]] = set()
    for entry in raw_entries:
        fallback_profile: str | None = None
        command: str | None = None
        if isinstance(entry, str):
            fallback_profile = entry
            command = configured_profile_command(config, fallback_profile)
            if command is None and (" " in entry or entry.startswith("/") or entry.startswith(".")):
                # Support direct command strings for local overrides while still
                # treating simple strings as profile names by default.
                fallback_profile = None
                command = entry
        elif isinstance(entry, dict):
            raw_profile = entry.get("profile") or entry.get("name")
            fallback_profile = str(raw_profile) if raw_profile else None
            raw_command = entry.get("command")
            command = str(raw_command) if raw_command else configured_profile_command(config, fallback_profile)
        if not command:
            continue
        key = (fallback_profile, command)
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, str] = {"command": command}
        if fallback_profile:
            item["profile"] = fallback_profile
        fallbacks.append(item)
    return fallbacks

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
    profiles = adapter_profiles(config)
    complex_default = "glm" if "glm" in profiles else ("codex-executor" if "codex-executor" in profiles else "complex")
    executor_default = "glm" if "glm" in profiles else "cheap"
    complex_profile = str(auto.get("complex_executor_profile") or auto.get("complexExecutorProfile") or complex_default)
    default_profile = str(auto.get("default_executor_profile") or auto.get("defaultExecutorProfile") or executor_default)
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
    if getattr(args, "executor_profile", None):
        args.executor_fallbacks = configured_executor_fallbacks(config, getattr(args, "executor_profile", None))
    elif not hasattr(args, "executor_fallbacks"):
        args.executor_fallbacks = []
    if not getattr(args, "reviewer_command", None):
        command = configured_reviewer_command(config, getattr(args, "reviewer_profile", None))
        if command:
            args.reviewer_command = command
