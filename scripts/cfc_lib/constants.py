from __future__ import annotations

VERSION = "0.7.0"
CFC_DIR = ".cfc"
TRACKED_CONFIG_FILE = "cfc.config.json"
DEFAULT_FORBIDDEN_PATHS = [
    "AGENTS.md",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "bun.lockb",
]
DEFAULT_FORBIDDEN_ACTIONS = [
    "commit",
    "push",
    "dependency_install",
    "format_entire_repo",
    "broad_refactor",
]
DEFAULT_IGNORED_STATUS_PATTERNS = [
    CFC_DIR,
    f"{CFC_DIR}/**",
    "**/__pycache__/**",
    "**/__pycache__/",
    "*.pyc",
    "**/*.pyc",
]

TRUE_STRINGS = {"1", "true", "True", "yes", "on"}
FALSE_STRINGS = {"0", "false", "False", "no", "off"}
DEFAULT_CHEAP_EXECUTOR_COMMAND = "opencode run --model kimi-k2.7-code -"
DEFAULT_COMPLEX_EXECUTOR_COMMAND = "opencode run --model glm-5.2 -"
DEFAULT_FRONTIER_EXECUTOR_COMMAND = "codex exec --dangerously-bypass-approvals-and-sandbox -"
DEFAULT_CODEX_REVIEWER_COMMAND = "codex exec --sandbox read-only -"
