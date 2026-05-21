"""Tests for scripts/peer-review.py — the cross-model code-review tool.

Covers the pure, network-free logic: prompt construction, the OpenRouter
request body (model ids + per-model params), and response parsing
(content / reasoning / error fallbacks). The actual codex CLI and OpenRouter
calls are not exercised here (they need the binary + a live key).
"""
import importlib.util
import sys
from pathlib import Path

# peer-review.py is hyphen-named — load it via importlib (matches the other
# scripts/ test modules).
_SCRIPT_PATH = Path(__file__).resolve().parent / "peer-review.py"
_spec = importlib.util.spec_from_file_location("peer_review", _SCRIPT_PATH)
peer_review = importlib.util.module_from_spec(_spec)
sys.modules["peer_review"] = peer_review
_spec.loader.exec_module(peer_review)


def test_three_reviewers_configured():
    # The two named OpenRouter models, in order; codex is the local CLI.
    ids = [m["id"] for m in peer_review.OR_MODELS]
    assert ids == ["qwen/qwen3.6-max-preview", "tencent/hy3-preview"]
    assert peer_review.CODEX_CMD[:2] == ["codex", "exec"]
    assert "read-only" in peer_review.CODEX_CMD


def test_tencent_uses_high_reasoning():
    tencent = next(m for m in peer_review.OR_MODELS if m["name"] == "tencent-hy3")
    assert tencent["extra"]["reasoning"]["effort"] == "high"


def test_build_prompt_codex_invites_repo_read():
    p = peer_review.build_prompt("DIFF", "logic", "main...HEAD", for_codex=True)
    assert "read the changed files" in p
    assert "main...HEAD" in p
    assert "DIFF" in p
    assert "FOCUS: logic" in p


def test_build_prompt_or_says_no_repo_access():
    p = peer_review.build_prompt("DIFF", "logic", "main...HEAD", for_codex=False)
    assert "cannot" in p and "read the repo" in p
    assert "DIFF" in p


def test_build_prompt_default_focus():
    p = peer_review.build_prompt("d", "", "x", for_codex=False)
    assert "correctness" in p.lower()


def test_or_request_body_carries_model_and_reasoning():
    tencent = next(m for m in peer_review.OR_MODELS if m["name"] == "tencent-hy3")
    body = peer_review.or_request_body(tencent, "PROMPT")
    assert body["model"] == "tencent/hy3-preview"
    assert body["messages"][0]["content"] == "PROMPT"
    assert body["reasoning"] == {"effort": "high"}


def test_parse_or_response_prefers_content():
    payload = {"choices": [{"message": {"content": "the review"}}]}
    assert peer_review.parse_or_response(payload) == "the review"


def test_parse_or_response_falls_back_to_reasoning():
    payload = {"choices": [{"message": {"content": "", "reasoning": "deep thoughts"}}]}
    out = peer_review.parse_or_response(payload)
    assert "deep thoughts" in out


def test_parse_or_response_surfaces_error():
    payload = {"error": {"message": "model not found"}}
    out = peer_review.parse_or_response(payload)
    assert "model not found" in out
