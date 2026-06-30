# Crazy for Codex (CfC)

CfC is a **headless, local-first recursive controller** for GJC/Codex/OMX-style coding agents.

It does not own a TUI. UI belongs to the host app/plugin; CfC owns the run ledger, guardrails, checks, review/repair loop, and learning artifacts.

```text
Codex / OMX / GJC plugin UI
  -> cfc plugin run/status/events/cancel
  -> CfC controller
  -> executor adapter + independent reviewer adapter
  -> check / review / repair / learn
```

CfC does **not** mutate `.gjc/` state. `.gjc/` remains GJC runtime state; `.cfc/` is the external control/learning layer.

## Plugin surface

Machine-readable manifest:

```bash
cfc plugin manifest
```

Run a task from a host plugin:

```bash
cfc plugin run "Fix small UI issue" \
  --root /path/to/repo \
  --allow 'src/Foo.tsx' \
  --verify 'npm run lint' \
  --executor-target gjc:0.0 \
  --reviewer-target gjc:0.1
```

Status/events/cancel for host UIs:

```bash
cfc plugin status --root /path/to/repo
cfc plugin events --root /path/to/repo
cfc plugin cancel --root /path/to/repo
```

All plugin status outputs are JSON so Codex/OMX/GJC adapters can render their own UI.

Workspace roots that are not git repositories are selector surfaces, not run targets. In that case `cfc plugin status --root <workspace>` returns `error: not_a_git_repository` plus `nested_git_roots`; adapters must choose one or more explicit nested repo roots and run CfC separately per repo. `cfc plugin run` refuses before creating `.cfc/` when the selected root is not a git repo.

## Headless CLI defaults

No-arg `cfc` prints help. It no longer opens a prompt/TUI.

A bare request still works as a shorthand for plugin-style loop execution:

```bash
cfc "README 정리해줘" --root /path/to/repo
```

Default bare/plugin loop settings can be controlled by tracked `cfc.config.json`
plus optional local `.cfc/config.local.json` overrides. This repository ships a
cost-optimized command-mode default:

- executor profile `auto`
  - all executor tasks -> `glm` -> `gjc -p --model opencode-go/glm-5.2 --no-session @{prompt_file}`
  - GLM command failure (quota/rate/auth/timeout/nonzero exit) -> `codex-executor` fallback
- reviewer profile `codex` -> `codex exec --sandbox read-only -`

The reviewer remains the only final PASS/REVIEW_BLOCKED authority; OpenCode Go models
are reached through GJC and used only as executors. `{prompt_file}` is expanded by CfC
to a temporary prompt file so GJC can receive long prompts as `@file` input instead
of oversized shell argv/stdin assumptions.

```json
{
  "adapters": {
    "mode": "command",
    "executor_profile": "auto",
    "reviewer_profile": "codex",
    "profiles": {
      "glm": { "command": "gjc -p --model opencode-go/glm-5.2 --no-session @{prompt_file}" },
      "codex-executor": { "command": "codex exec --dangerously-bypass-approvals-and-sandbox -" },
      "codex": { "command": "codex exec --sandbox read-only -" }
    },
    "auto": {
      "default_executor_profile": "glm",
      "complex_executor_profile": "glm"
    },
    "fallbacks": { "glm": ["codex-executor"] }
  }
}
```

Legacy environment variables are still accepted as fallbacks:

```text
CFC_EXECUTOR_COMMAND
CFC_REVIEWER_COMMAND
CFC_EXECUTOR_TARGET       default: gjc:0.0
CFC_REVIEWER_TARGET       default: cfc-review:0.0
CFC_SEND                  default: 1
CFC_TMUX_WAIT_SECONDS     default: 0
CFC_MAX_ITERATIONS        default: 3
CFC_APPLY_LEARN           default: 0
CFC_ISOLATED_TMUX         default: 1
CFC_REVIEW_POLL_SECONDS   default: 5
CFC_REVIEW_WAIT_TIMEOUT_SECONDS default: 0 (wait indefinitely)
```

With the default tmux mode and `CFC_TMUX_WAIT_SECONDS=0`, CfC dispatches one step and returns with `RUN.json.awaiting` set. Resume the active run with `cfc capture --root <repo>` after the external agent finishes.

## Core loop

The loop requires an executor adapter and an independent reviewer adapter. Use either:

- tmux/GJC mode: `--send`, `--executor-target`, `--reviewer-target`
- command mode: `--executor-command`, `--reviewer-command`

```bash
python3 scripts/cfc.py loop --root /path/to/repo \
  "Fix small UI issue" \
  --allow 'src/Foo.tsx' \
  --verify 'npm run lint' \
  --executor-profile glm \
  --reviewer-profile codex
```

Synchronous command-mode flow:

```text
executor -> diff/check -> independent review -> classify -> repair if blocked -> learn -> done if clean
```

Async tmux/GJC flow:

```text
executor prompt sent
  -> cfc capture
  -> diff/check
  -> reviewer prompt sent
  -> cfc capture --wait-verdict
  -> classify review
  -> if PASS: ready for cfc done
  -> if REVIEW_BLOCKED: send BLOCKERS back to executor as REPAIR_PROMPT
  -> cfc capture
  -> diff/check
  -> review again
```

The reviewer must end with a strict final verdict line:

```text
Verdict: PASS
```

or:

```text
Verdict: REVIEW_BLOCKED
```

Missing or malformed verdict lines are treated as `REVIEW_BLOCKED`, never `PASS`.

Reviewer evidence includes `git status`, unstaged diff, staged diff, small text untracked files, and `CHECK.md` verification evidence.

## DONE.md ownership

CfC owns final artifacts under:

```text
.cfc/runs/<run-id>/DONE.md
```

Workers must not create final reports in the repository root. A newly-created root-level `DONE.md` fails `cfc check`, and an existing dirty root-level `DONE.md` makes `cfc start` refuse even with `--allow-dirty`.

Nested or legitimate project files named `DONE.md` are not globally forbidden. For example, `src/DONE.md` is allowed when it is inside the run's allowed paths.

## Learning

`done` and `learn` are separate concerns:

```text
PASS review -> LEARN.md -> high-confidence wiki entries -> DONE.md
REVIEW_BLOCKED -> LEARN.md -> high-confidence process/system failures may still enter .cfc/wiki -> no DONE.md
```

This means blocked/failed runs can still preserve useful process lessons without falsely marking the task complete.

## Manual primitives

```bash
python3 scripts/cfc.py init --root /path/to/repo
python3 scripts/cfc.py start --root /path/to/repo "Fix small UI issue" --allow 'src/Foo.tsx' --verify 'npm run lint'
python3 scripts/cfc.py gjc --root /path/to/repo "Implement with minimal diff" --tmux-target gjc:0.0 --send
python3 scripts/cfc.py capture --root /path/to/repo
python3 scripts/cfc.py check --root /path/to/repo
python3 scripts/cfc.py review --root /path/to/repo --tmux-target gjc:0.1 --send
python3 scripts/cfc.py classify-review --root /path/to/repo
python3 scripts/cfc.py repair --root /path/to/repo --send
python3 scripts/cfc.py learn --root /path/to/repo
python3 scripts/cfc.py done --root /path/to/repo
```

If the worktree is already dirty, `cfc start` refuses by default. Use `--allow-dirty` only when you intentionally accept the current dirty files as baseline evidence.

If a run is already active, `cfc start` refuses by default. Use `--replace` only when you intentionally supersede the active run pointer.

## Commands

```text
plugin          machine-readable adapter surface: manifest/run/status/events/cancel
init            initialize .cfc state in a target repo
start           start a guarded run
status          show active run and recent ledger events
gjc             generate guarded executor prompt, optionally send to tmux
capture         capture GJC/tmux output; resumes awaiting executor/reviewer steps
check           run git scope checks and verification commands
diff            write DIFF.md from current git diff
review          generate/send read-only independent review prompt
classify-review parse REVIEW.iteration-N.md into BLOCKERS.md and LEARN.md
repair          generate/send repair-only prompt from current blockers
loop            execute -> check -> fresh review -> repair until blocker-free -> learn
park            add a note to PARKING_LOT.md
learn           generate/apply LLM-Wiki-style learning candidates
done            finalize a run after checks and independent review
events          print ledger.jsonl events
```

## Data model

```text
.cfc/
  config.json
  current.json
  runs/<run-id>/
    RUN.json
    TASK.md
    PRECHECK.md
    PROMPT.iteration-1.md
    GJC_LOG.<time>.md
    GJC_LOG.iteration-1.md
    EXECUTION.iteration-1.md        # command-mode executor output
    EXECUTION.iteration-1.fallback-1.md # optional executor fallback output
    DIFF.md
    CHECK.md
    REVIEW_PROMPT.iteration-1.md
    REVIEW.iteration-1.md
    BLOCKERS.md
    REPAIR_PROMPT.iteration-1.md
    LEARN.md
    DONE.md                        # only when complete
    PARKING_LOT.md
    ledger.jsonl
  wiki/
    index.md
    log.md
    failures/*.md
    guardrails/*.md
    patterns/*.md
    runbooks/*.md
    checklists/*.md
```

## Development

```bash
python3 -m unittest discover -s tests -v
```
