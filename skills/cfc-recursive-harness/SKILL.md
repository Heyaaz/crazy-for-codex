---
name: cfc-recursive-harness
description: Use CfC to wrap GJC with run ledgers, QA loops, guardrails, and LLM-Wiki-style learning.
---

# CfC Recursive Harness

Use this skill when running GJC through CfC.

## Workflow

1. `cfc init` in the target repository.
2. `cfc start "task"` with allowed paths and verification commands.
3. `cfc gjc "request" --send` to generate and optionally inject a guarded prompt into a GJC tmux pane.
4. `cfc capture` to save the visible GJC terminal transcript.
5. `cfc check` to verify git scope, forbidden paths, diff, and configured commands.
6. `cfc review` to generate a read-only independent review prompt.
7. `cfc learn` to extract learning candidates from blockers/checks.
8. `cfc done` only after checks pass or with explicit `--force`.

## Rules

- Keep `.gjc/` owned by GJC; CfC writes `.cfc/` only.
- Treat GJC's self-report as untrusted until CfC check/review passes.
- Use `learn --apply` only after human approval or when the run evidence is clear.
- Prefer active guardrails from `.cfc/wiki/guardrails` in future prompts.
