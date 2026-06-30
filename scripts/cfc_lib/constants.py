from __future__ import annotations

VERSION = "0.7.0"
CFC_DIR = ".cfc"
TRACKED_CONFIG_FILE = "cfc.config.json"
DEFAULT_FORBIDDEN_PATHS = [
    "AGENTS.md",
    "bun.lockb",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
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
DEFAULT_GLM_EXECUTOR_COMMAND = "gjc -p --model opencode-go/glm-5.2 --no-session @{prompt_file}"
DEFAULT_CODEX_EXECUTOR_COMMAND = "codex exec --dangerously-bypass-approvals-and-sandbox -"
DEFAULT_CHEAP_EXECUTOR_COMMAND = DEFAULT_GLM_EXECUTOR_COMMAND
DEFAULT_COMPLEX_EXECUTOR_COMMAND = DEFAULT_GLM_EXECUTOR_COMMAND
DEFAULT_FRONTIER_EXECUTOR_COMMAND = DEFAULT_CODEX_EXECUTOR_COMMAND
DEFAULT_CODEX_REVIEWER_COMMAND = "codex exec --sandbox read-only -"
