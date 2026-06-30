from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path


def command_looks_like_gjc_rpc(command: str) -> bool:
    return bool(re.search(r"(^|\s)--mode(?:=|\s+)rpc(\s|$)", command))


def _reader(stream, out: queue.Queue[str]) -> None:
    try:
        for line in stream:
            out.put(line)
    finally:
        out.put("")


def _send(proc: subprocess.Popen[str], frame: dict) -> None:
    if proc.stdin is None:
        raise RuntimeError("rpc stdin is closed")
    proc.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def _event_kind(frame: dict) -> str:
    event = frame.get("event") if isinstance(frame.get("event"), dict) else frame
    return str(event.get("kind") or event.get("type") or frame.get("kind") or "")


def _is_completion_event(frame: dict) -> bool:
    return _event_kind(frame) in {"agent_end", "agent_completed", "rpc_agent_completed"}


def run_gjc_rpc_command(command: str, prompt: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        command,
        cwd=str(cwd),
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=True,
    )
    stdout_q: queue.Queue[str] = queue.Queue()
    stderr_q: queue.Queue[str] = queue.Queue()
    assert proc.stdout is not None
    assert proc.stderr is not None
    threading.Thread(target=_reader, args=(proc.stdout, stdout_q), daemon=True).start()
    threading.Thread(target=_reader, args=(proc.stderr, stderr_q), daemon=True).start()

    frames: list[dict] = []
    raw_stdout: list[str] = []
    prompt_sent = False
    completion_seen = False
    last_text: str | None = None
    prompt_error: str | None = None
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            try:
                line = stdout_q.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            if line == "":
                if proc.poll() is not None:
                    break
                continue
            raw_stdout.append(line)
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue
            frames.append(frame)
            if frame.get("type") == "ready" and not prompt_sent:
                _send(proc, {"id": "cfc-prompt", "type": "prompt", "message": prompt})
                prompt_sent = True
                continue
            if frame.get("type") == "response" and frame.get("id") == "cfc-prompt" and frame.get("success") is False:
                prompt_error = json.dumps(frame.get("error"), ensure_ascii=False)
                break
            if _is_completion_event(frame) and not completion_seen:
                completion_seen = True
                _send(proc, {"id": "cfc-last", "type": "get_last_assistant_text"})
                continue
            if frame.get("type") == "response" and frame.get("id") == "cfc-last":
                data = frame.get("data") if isinstance(frame.get("data"), dict) else {}
                value = data.get("text") if isinstance(data, dict) else None
                last_text = str(value or "")
                break
        else:
            prompt_error = f"gjc rpc command timed out after {timeout} seconds"
    finally:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    stderr_lines: list[str] = []
    while not stderr_q.empty():
        line = stderr_q.get_nowait()
        if line:
            stderr_lines.append(line)
    stderr = "".join(stderr_lines)
    if prompt_error:
        stderr = (stderr + "\n" if stderr else "") + prompt_error
    stdout = last_text if last_text is not None else "".join(raw_stdout)
    return subprocess.CompletedProcess(command, 1 if prompt_error else (proc.returncode or 0), stdout, stderr)
