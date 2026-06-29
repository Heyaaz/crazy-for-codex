# Crazy for Codex (CfC)

CfC is a local-first recursive harness for GJC/Codex-style coding agents.

It keeps GJC as the worker and adds an external control/learning layer:

- run records under `.cfc/runs/`
- observable loop ledger (`ledger.jsonl`)
- guarded GJC prompt generation and optional tmux injection
- real git scope checks and verification command evidence
- read-only review prompt generation and review-result classification
- automatic `loop`: execute → check → clean review → repair → learn
- LLM-Wiki-style learning candidates under `.cfc/wiki/`

CfC does **not** mutate `.gjc/` state. `.gjc/` remains GJC runtime state; `.cfc/` is the external learning/control layer.

## Conversational mode

For daily use, run `cfc` with no subcommand:

```bash
cd /path/to/repo
cfc
```

Then type naturally:

```text
cfc> README 정리해줘
cfc> /status
cfc> /exit
```

You can also pass a request directly:

```bash
cfc "README 정리해줘" --root /path/to/repo
```

By default, conversational requests run the recursive loop with:

- `allow: *`
- `verify: git diff --check`
- executor target: `$CFC_EXECUTOR_TARGET` or `gjc:0.0`
- reviewer target: `$CFC_REVIEWER_TARGET` or `cfc-review:0.0`

## Quick start

```bash
python3 scripts/cfc.py init --root /path/to/repo

python3 scripts/cfc.py loop --root /path/to/repo \
  "Fix small UI issue" \
  --allow 'src/Foo.tsx' \
  --verify 'npm run lint' \
  --executor-target gjc:0.0 \
  --reviewer-target gjc:0.1 \
  --send \
  --tmux-wait-seconds 120
```

The loop requires an executor adapter and an independent reviewer adapter. In tmux mode, pass `--send` with separate `--executor-target` / `--reviewer-target`. In non-interactive test/CI mode, pass `--executor-command` and `--reviewer-command`.

The reviewer prompt includes `git status`, unstaged diff, staged diff, and small text untracked files so fresh review sees new files too.

The loop creates a run, sends an executor prompt, checks the real diff and verification evidence, opens a fresh read-only reviewer prompt, classifies BLOCKER findings, sends a repair prompt when needed, repeats up to `--max-iterations`, then generates `LEARN.md` and `DONE.md` when clean.

Manual primitives are also available:

```bash
python3 scripts/cfc.py start --root /path/to/repo \
  "Fix small UI issue" \
  --allow 'src/Foo.tsx' \
  --verify 'npm run lint'

python3 scripts/cfc.py gjc --root /path/to/repo \
  "Implement with minimal diff" \
  --tmux-target gjc:0.0 \
  --send

python3 scripts/cfc.py capture --root /path/to/repo --tmux-target gjc:0.0
python3 scripts/cfc.py check --root /path/to/repo
python3 scripts/cfc.py review --root /path/to/repo --tmux-target gjc:0.0 --send
python3 scripts/cfc.py learn --root /path/to/repo
python3 scripts/cfc.py done --root /path/to/repo
```

If the worktree is already dirty, `cfc start` refuses by default. Use `--allow-dirty` only when you intentionally accept the current dirty files as baseline evidence.

If a run is already active, `cfc start` refuses by default. Use `--replace` only when you intentionally supersede the active run pointer.

## Commands

```text
init      initialize .cfc state in a target repo
start     start a guarded run
status    show active run and recent ledger events
gjc       generate guarded executor prompt, optionally send to tmux
capture   capture GJC/tmux output into the run folder
check     run git scope checks and verification commands
diff      write DIFF.md from current git diff
review    generate read-only independent review prompt
classify-review parse REVIEW.iteration-N.md into BLOCKERS.md
repair    generate/send repair-only prompt from current blockers
loop      execute → check → fresh review → repair until blocker-free → learn
park      add a note to PARKING_LOT.md
learn     generate/apply LLM-Wiki-style learning candidates
done      finalize a run after checks
events    print ledger.jsonl events
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

## Philosophy

```text
GJC = worker / executor / reviewer
CfC = external run ledger + guardrails + checks + learning wiki
```

CfC grows by turning repeated blockers into local Markdown knowledge:

```text
run evidence -> LEARN.md -> wiki/failures + wiki/guardrails + wiki/runbooks -> future prompts/checks
```

Learning is suggestion-first. Use `learn --apply` only when you want CfC to write wiki entries.

## Development

Run tests:

```bash
python3 -m unittest discover -s tests -v
```
