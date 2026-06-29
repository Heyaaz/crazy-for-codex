# CfC Recursive Harness PRD

## Product

CfC (Crazy for Codex) is a local-first recursive harness that wraps GJC without owning or mutating GJC internals.

```text
User -> CfC -> GJC -> repo changes -> CfC check/review/learn -> CfC Wiki -> next prompt/check
```

## Problem

Using GJC directly leaves three recurring gaps:

1. Implementation can finish while QA/code review still finds BLOCKER issues.
2. Long contexts can compress poorly and lose the task thread.
3. The running loop is not observable enough from outside.

## Goal

Make GJC work evidence-backed and self-improving by adding an external layer that:

- records every work run under `.cfc/runs/`
- generates guarded GJC prompts
- checks real git diff and verification output
- supports independent read-only review prompts
- extracts learn candidates from failures/blockers
- stores reusable knowledge in an LLM-Wiki-style `.cfc/wiki/`

## Non-goals

- Do not modify GJC internals.
- Do not write `.gjc/` state.
- Do not auto-commit, push, or open PRs.
- Do not run infinite autonomous loops in MVP.
- Do not require external APIs or a DB.

## MVP Commands

```bash
cfc
cfc "task" --root /path/to/repo
cfc init
cfc start "task" --allow <path> --forbid <path> --verify "command"
# refuses dirty worktrees by default; use --allow-dirty to accept baseline dirt
# refuses to overwrite active runs by default; use --replace intentionally
cfc loop "task" --allow <path> --verify "command" --executor-target gjc:0.0 --reviewer-target gjc:0.1 --send
cfc status
cfc gjc "request" [--send --tmux-target gjc:0.0]
cfc capture [--tmux-target gjc:0.0]
cfc check
cfc diff
cfc review [--send --tmux-target gjc:0.0]
cfc park "note"
cfc learn [--apply]
cfc done [--force]
cfc events
```

## Loop Contract

`cfc loop` requires both an executor and an independent reviewer adapter before it mutates a target repository. It refuses to run without either:

- tmux mode: `--send --executor-target <tmux-pane> --reviewer-target <tmux-pane>`
- command mode: `--executor-command <cmd> --reviewer-command <cmd>`

The review evidence includes `git status`, unstaged diff, staged diff, and small text untracked files. Reviewer failure or empty reviewer output is treated as `REVIEW_BLOCKED`, never PASS.

## Data Model

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
    REVIEW_PROMPT.iteration-1.md
    REVIEW.iteration-1.md
    BLOCKERS.md
    REPAIR_PROMPT.iteration-1.md
    DIFF.md
    CHECK.md
    LEARN.md
    DONE.md
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

## Guardrail Rules

A run can define:

- `allowed_paths`: changed files must match these paths/globs if present.
- `forbidden_paths`: any changed file matching these is a FAIL.
- `verification.commands`: each command must exit 0 for PASS.

Default forbidden paths include common surprise files:

- `AGENTS.md`
- `package-lock.json`
- `yarn.lock`
- `pnpm-lock.yaml`
- `bun.lockb`

## Learning Loop

CfC treats blockers as learning material.

```text
CHECK/REVIEW evidence
-> cfc learn
-> LEARN.md suggestions
-> cfc learn --apply
-> .cfc/wiki/failures|guardrails|runbooks
-> future cfc gjc prompts include active wiki knowledge
```

Learning is suggestion-first. `--apply` writes Markdown entries, but entries keep `source_runs` and warn that evidence should be reviewed before becoming a strong rule.

## Done Criteria

A run is done only when:

- `CHECK.md` exists or user forces completion
- check verdict is not FAIL, unless forced
- changed files and verification evidence are recorded
- `DONE.md` exists

## Future Versions

- `cfc loop`: execute -> check -> review -> repair iterations.
- `cfc repair`: generate repair-only prompt from BLOCKER findings.
- command-agent mode for deterministic tests and non-interactive agent adapters.
- tmux mode for existing GJC/Ghostty panes, with separate executor/reviewer targets.
- coordinator adapter using `gjc_coordinator_*` tools instead of tmux.
- read-only ingestion of `.gjc/ultragoal` and `.gjc/team` evidence.
- stronger wiki retrieval by task tags.
- stricter anti-feedback rules to avoid learning from previously injected wiki text.
