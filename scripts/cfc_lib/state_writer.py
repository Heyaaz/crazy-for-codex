from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _inside(parent: Path, child: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _lexical_cfc_root(path: Path) -> Path | None:
    parts = path.parts
    for index, part in enumerate(parts):
        if part == ".cfc":
            return Path(*parts[: index + 1])
    return None


def validate_state_path(path: Path) -> None:
    """Reject symlink/path escapes for writes lexically under `.cfc`.

    CfC state paths are normally under `.cfc/**`. For compatibility this
    function leaves non-CfC paths alone, but any path that claims to be under
    `.cfc` must resolve back inside that same `.cfc` root.
    """
    cfc_root = _lexical_cfc_root(path)
    if cfc_root is None:
        return
    cfc_root.parent.mkdir(parents=True, exist_ok=True)
    if not cfc_root.exists():
        cfc_root.mkdir(parents=True, exist_ok=True)
    target_parent = path.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    if not _inside(cfc_root, target_parent):
        raise ValueError(f"refusing to write CfC state outside .cfc root: {path}")


def write_text_atomic(path: Path, text: str) -> None:
    validate_state_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if fd != -1:
            os.close(fd)
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass


def write_json_atomic(path: Path, data: Any) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, data: Any) -> None:
    validate_state_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(data, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
