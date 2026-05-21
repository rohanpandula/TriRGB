# Code-Review Policy — cross-model peer gate (v1.2)

Every v1.2 phase that lands code passes a **three-reviewer cross-model peer
pass** before it is signed off. This is requirement **NFR-15** and applies to
all v1.2 phases (08–15). It runs in addition to — not instead of — the normal
`gsd-code-review` pass.

## The three reviewers

| Reviewer | Transport | Nature |
|----------|-----------|--------|
| **Codex** | local Codex CLI (`codex exec -s read-only`) | Agent — reads the actual changed files in the repo for context. The deepest reviewer. |
| **qwen** | `qwen/qwen3.6-max-preview` via OpenRouter | One-shot; judges from the diff alone. |
| **tencent** | `tencent/hy3-preview` (reasoning: high) via OpenRouter | One-shot; judges from the diff alone. |

Three independent models catch different things; the spread is the point.

## When

During each phase's code-review step — **after** `gsd-code-review` produces its
findings, **before** the phase is marked done. Concurrency-heavy or
correctness-critical changes especially (this codebase has a history of
absence-of-mechanism bugs that per-file review misses).

## How

```bash
export OPENROUTER_API_KEY=...        # the two OpenRouter reviewers need this; never commit it
scripts/peer-review.py --focus "what to scrutinize" --range <phase-base>...HEAD
# or: git diff <range> | scripts/peer-review.py --focus "..."
scripts/peer-review.py --check       # verify reviewers are available first
```

`peer-review.py` prints each reviewer's critique. Codex needs the `codex` CLI on
PATH; the OpenRouter pair needs `OPENROUTER_API_KEY` in the environment (read
from env only — the key is never printed or written to disk; `.env` is
gitignored). If a reviewer is unavailable it's skipped with a note and the
others still run.

## How to weigh the findings (do NOT rubber-stamp)

1. Read each critique in full.
2. **Cross-check concrete claims against the actual code** — all three models
   can be wrong or overstate (e.g. a prior pass wrongly claimed "NLP can't
   process the file" and "B&W will clip"; both were rejected on inspection).
3. Three models agreeing on a wrong answer is still wrong — the gate is a hedge
   against blind spots, not an outsourced decision.
4. Apply what's right; note and move past what's wrong or shallow.
5. **Record the outcome in the phase SUMMARY**: what each reviewer flagged,
   what was incorporated, what was rejected and why.

## Model ids

Confirmed live on OpenRouter as of 2026-05-21: `qwen/qwen3.6-max-preview`,
`tencent/hy3-preview`. If an id changes, edit `OR_MODELS` at the top of
`scripts/peer-review.py`. Codex is whatever the local `codex` CLI resolves to.
