---
name: cfc-recursive-harness
description: Use CfC to wrap GJC with run ledgers, QA loops, guardrails, and LLM-Wiki-style learning.
---

# CfC Recursive Harness

Use this skill when running GJC through CfC.

## Workflow

1. `cfc init` in the target repository.
2. `cfc start "task"` with allowed paths and verification commands.
3. `cfc loop "task" --send` to generate executor prompts, check real evidence, open fresh reviewer prompts, repair blockers, then learn.
4. For manual operation, use `cfc gjc`, `cfc capture`, `cfc check`, `cfc review`, `cfc classify-review`, `cfc repair`, `cfc learn`, and `cfc done`.

## Rules

- Keep `.gjc/` owned by GJC; CfC writes `.cfc/` only.
- Treat GJC's self-report as untrusted until CfC check/review passes.
- Use `learn --apply` only after human approval or when the run evidence is clear.
- Prefer active guardrails from `.cfc/wiki/guardrails` in future prompts.
