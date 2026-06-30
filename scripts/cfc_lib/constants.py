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

# Token/context budget presets, selected via `--budget light|normal|strict` or
# `CFC_BUDGET`. Each preset tunes wiki char budget, default tmux capture lines,
# and whether the reviewer risk-gate/cheap-reviewer may auto-PASS low-risk runs.
BUDGET_PRESETS = {
    "light": {
        "wiki_chars": 1500,
        "capture_lines": 1000,
        "review_risk_gate": True,
    },
    "normal": {
        "wiki_chars": 2000,
        "capture_lines": 1000,
        "review_risk_gate": True,
    },
    "strict": {
        "wiki_chars": 6000,
        "capture_lines": 5000,
        "review_risk_gate": False,
    },
}
DEFAULT_BUDGET = "normal"
