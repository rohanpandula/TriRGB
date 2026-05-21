#!/usr/bin/env python3
"""Cross-model peer review for the v1.2 code-review gate.

Runs THREE independent reviewers over a diff and prints their critiques side by
side so the orchestrator can weigh them (NOT rubber-stamp — see docs/REVIEW-POLICY.md):

  - codex    : the local Codex CLI (`codex exec -s read-only`). Agent-capable —
               it reads the actual changed files in the repo for context.
  - qwen     : qwen/qwen3.6-max-preview        via OpenRouter (one-shot, diff inline)
  - tencent  : tencent/hy3-preview (reasoning: high) via OpenRouter (one-shot)

The two OpenRouter reviewers need an API key in the OPENROUTER_API_KEY
environment variable. The key is never printed or written to disk. Codex needs
the `codex` CLI on PATH (the local install). Each reviewer is independent: if
one is unavailable it is skipped with a clear note and the others still run.

Usage:
    scripts/peer-review.py --focus "what to scrutinize" [--range main...HEAD]
    scripts/peer-review.py --focus "..." --diff some.diff
    git diff | scripts/peer-review.py --focus "..."
    scripts/peer-review.py --check        # report which reviewers are available
    scripts/peer-review.py --dry-run --focus "..."   # show prompts, make no calls

Diff source precedence: --diff FILE  >  --range GIT_RANGE  >  piped stdin  >  `git diff HEAD`.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# OpenRouter reviewers. Edit ids/params here if they change.
OR_MODELS = [
    {"name": "qwen", "id": "qwen/qwen3.6-max-preview", "extra": {}},
    {"name": "tencent-hy3", "id": "tencent/hy3-preview", "extra": {"reasoning": {"effort": "high"}}},
]

# Local Codex CLI invocation (read-only sandbox, no git-repo gate).
CODEX_CMD = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check"]

CODEX_TIMEOUT_S = 420
OR_TIMEOUT_S = 240


def get_diff(args) -> tuple[str, str]:
    """Resolve the diff text and a human label for what's being reviewed."""
    if args.diff:
        with open(args.diff, "r", encoding="utf-8") as f:
            return f.read(), f"--diff {args.diff}"
    if args.range:
        out = subprocess.run(
            ["git", "diff", args.range], capture_output=True, text=True, check=False
        )
        return out.stdout, f"git diff {args.range}"
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped, "piped stdin"
    out = subprocess.run(
        ["git", "diff", "HEAD"], capture_output=True, text=True, check=False
    )
    return out.stdout, "git diff HEAD"


def build_prompt(diff: str, focus: str, label: str, *, for_codex: bool) -> str:
    """Build the review prompt. Codex gets repo-read guidance; OR models get the
    diff inline (they cannot read the repo)."""
    focus = focus.strip() or "correctness, bugs, logic errors, missed edge cases, regressions"
    header = (
        "READ-ONLY peer review of a code change. Do NOT propose edits to apply — "
        "just review. Report findings as a short, severity-ranked list "
        "(BLOCKER / MAJOR / MINOR / NIT) with file:line where possible, then a "
        "one-line overall verdict. Be concise and concrete.\n\n"
        f"FOCUS: {focus}\n\n"
    )
    if for_codex:
        return (
            header
            + f"The change under review is `{label}`. You may read the changed files "
            "in the repo for context (read-only). The diff is below.\n\n"
            "```diff\n" + diff + "\n```\n"
        )
    return (
        header
        + f"The change under review ({label}) is the following diff (you cannot "
        "read the repo — judge from the diff alone).\n\n"
        "```diff\n" + diff + "\n```\n"
    )


def or_request_body(model: dict, prompt: str) -> dict:
    """Build the OpenRouter chat/completions request body for a reviewer."""
    body = {
        "model": model["id"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 6000,
    }
    body.update(model.get("extra", {}))
    return body


def parse_or_response(payload: dict) -> str:
    """Extract the reviewer's text (content, else reasoning, else error)."""
    try:
        msg = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        err = (payload or {}).get("error", {}).get("message")
        return f"[no choices] {err or json.dumps(payload)[:500]}"
    content = (msg.get("content") or "").strip()
    if content:
        return content
    reasoning = (msg.get("reasoning") or "").strip()
    if reasoning:
        return "[answer in reasoning field]\n" + reasoning
    return "[empty response]"


def run_or_model(model: dict, prompt: str, key: str) -> str:
    body = json.dumps(or_request_body(model, prompt)).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OR_TIMEOUT_S) as resp:
            return parse_or_response(json.loads(resp.read().decode("utf-8")))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        return f"[HTTP {e.code}] {detail}"
    except Exception as exc:  # noqa: BLE001 — surface, don't crash the whole review
        return f"[request failed] {type(exc).__name__}: {exc}"


def run_codex(prompt: str) -> str:
    if shutil.which("codex") is None:
        return "[skipped] codex CLI not found on PATH"
    try:
        out = subprocess.run(
            CODEX_CMD, input=prompt, capture_output=True, text=True,
            timeout=CODEX_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired:
        return f"[codex timed out after {CODEX_TIMEOUT_S}s]"
    if out.returncode != 0 and not out.stdout.strip():
        return f"[codex exit {out.returncode}] {out.stderr.strip()[:400]}"
    return out.stdout.strip() or out.stderr.strip() or "[codex returned no output]"


def cmd_check() -> int:
    codex_ok = shutil.which("codex") is not None
    key_ok = bool(os.environ.get("OPENROUTER_API_KEY"))
    print("Peer-review reviewer availability:")
    print(f"  codex (local CLI)       : {'OK' if codex_ok else 'MISSING (install codex)'}")
    print(f"  OPENROUTER_API_KEY      : {'set' if key_ok else 'UNSET (export it for the OR models)'}")
    for m in OR_MODELS:
        print(f"  {m['name']:<22}: {m['id']}" + (" (OR)" if key_ok else " (needs key)"))
    return 0 if (codex_ok or key_ok) else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="peer-review", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--focus", default="", help="What the reviewers should scrutinize.")
    p.add_argument("--range", help="git range to diff (e.g. main...HEAD).")
    p.add_argument("--diff", help="Path to a diff file to review.")
    p.add_argument("--check", action="store_true", help="Report reviewer availability and exit.")
    p.add_argument("--dry-run", action="store_true", help="Print the prompts; make no calls.")
    args = p.parse_args(argv)

    if args.check:
        return cmd_check()

    diff, label = get_diff(args)
    if not diff.strip():
        print("peer-review: empty diff — nothing to review "
              "(pass --range/--diff or pipe a diff).", file=sys.stderr)
        return 2

    key = os.environ.get("OPENROUTER_API_KEY", "")
    print(f"=== peer review of: {label}  ({len(diff.splitlines())} diff lines) ===\n")

    if args.dry_run:
        print("--- CODEX PROMPT ---\n" + build_prompt(diff, args.focus, label, for_codex=True))
        for m in OR_MODELS:
            print(f"\n--- {m['name']} ({m['id']}) PROMPT ---\n"
                  + build_prompt(diff, args.focus, label, for_codex=False))
        print("\n[dry-run] no calls made.")
        return 0

    ran_any = False

    # 1. Codex (local CLI, agent — reads the repo).
    print("================ CODEX (local CLI, read-only) ================")
    print(run_codex(build_prompt(diff, args.focus, label, for_codex=True)))
    ran_any = True
    print()

    # 2. OpenRouter reviewers.
    if not key:
        print("[skipped OR models] OPENROUTER_API_KEY is not set.\n", file=sys.stderr)
    else:
        for m in OR_MODELS:
            print(f"================ {m['name'].upper()} ({m['id']}) via OpenRouter ================")
            print(run_or_model(m, build_prompt(diff, args.focus, label, for_codex=False), key))
            ran_any = True
            print()

    print("--- NOTE: weigh these critiques, do not rubber-stamp. Cross-check concrete "
          "claims against the code; record agree/disagree + reasoning in the phase SUMMARY. ---")
    return 0 if ran_any else 1


if __name__ == "__main__":
    raise SystemExit(main())
