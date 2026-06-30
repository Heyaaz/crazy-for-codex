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

from .constants import CFC_DIR

def root_path(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "root", ".") or ".").expanduser().resolve()

def cfc_path(root: Path) -> Path:
    return root / CFC_DIR

def current_file(root: Path) -> Path:
    return cfc_path(root) / "current.json"

def runs_dir(root: Path) -> Path:
    return cfc_path(root) / "runs"

def wiki_dir(root: Path) -> Path:
    return cfc_path(root) / "wiki"

def ensure_cfc(root: Path) -> None:
    if not cfc_path(root).exists():
        raise SystemExit(f"CfC is not initialized in {root}. Run: cfc init --root {shlex.quote(str(root))}")
