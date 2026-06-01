"""Tests for scripts/check_docs_consistency.py.

Covers:
  - parse_swift_enum: extracts the expected AX-ID count from the real swift file
  - parse_swift_enum: excludes schemaVersion symbol and its value "1"
  - parse_md_reference: extracts the expected AX-ID count from the real markdown file
  - Real sources are consistent (live end-to-end check)
  - compare detects IDs present in swift but missing from md
  - compare detects IDs present in md but missing from swift
  - main() returns 0 against the canonical repo files
  - main() returns 1 against a synthetic md with a drift row

Run from the repo root:
  python3 -m pytest scripts/test_check_docs_consistency.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Insert scripts/ onto sys.path so `import check_docs_consistency` works
# regardless of the CWD pytest was invoked from. Same pattern as
# scripts/test_inspect_calibration.py.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_docs_consistency as cdc  # noqa: E402

_REPO_ROOT = _SCRIPTS_DIR.parent
_SWIFT_FILE = _REPO_ROOT / "phase3" / "FilmScanner" / "Sources" / "ScanlightApp" / "AccessibilityIDs.swift"
_MD_FILE = _REPO_ROOT / "docs" / "ax-id-reference.md"

# Current number of AX-IDs in AccessibilityIDs.swift (excluding schemaVersion).
# Bump this when AX-IDs are added/removed. The real drift gate is
# test_real_sources_are_consistent (set equality between the two sources); this
# constant is a parser sanity-check shared by the two count tests below.
_EXPECTED_AX_ID_COUNT = 133


# --------------------------------------------------------------------------
# parse_swift_enum
# --------------------------------------------------------------------------

def test_parse_swift_enum_extracts_expected_count():
    """The real AccessibilityIDs.swift should yield _EXPECTED_AX_ID_COUNT strings."""
    swift_text = _SWIFT_FILE.read_text()
    result = cdc.parse_swift_enum(swift_text)
    assert len(result) == _EXPECTED_AX_ID_COUNT, (
        f"expected {_EXPECTED_AX_ID_COUNT} AX-IDs from swift enum, got {len(result)}: {sorted(result)}"
    )


def test_parse_swift_enum_excludes_schemaVersion():
    """schemaVersion is not an AX-ID — neither the symbol name nor its value '1'
    should appear in the returned set."""
    swift_text = _SWIFT_FILE.read_text()
    result = cdc.parse_swift_enum(swift_text)
    assert "schemaVersion" not in result, (
        "schemaVersion symbol leaked into the AX-ID set"
    )
    assert "1" not in result, (
        'schemaVersion value "1" leaked into the AX-ID set'
    )


# --------------------------------------------------------------------------
# parse_md_reference
# --------------------------------------------------------------------------

def test_parse_md_reference_extracts_expected_count():
    """The real docs/ax-id-reference.md should yield _EXPECTED_AX_ID_COUNT strings."""
    md_text = _MD_FILE.read_text()
    result = cdc.parse_md_reference(md_text)
    assert len(result) == _EXPECTED_AX_ID_COUNT, (
        f"expected {_EXPECTED_AX_ID_COUNT} AX-IDs from markdown table, got {len(result)}: {sorted(result)}"
    )


# --------------------------------------------------------------------------
# End-to-end consistency
# --------------------------------------------------------------------------

def test_real_sources_are_consistent():
    """The live swift enum and the live markdown reference must agree on IDs."""
    swift_ids = cdc.parse_swift_enum(_SWIFT_FILE.read_text())
    md_ids = cdc.parse_md_reference(_MD_FILE.read_text())
    swift_only, md_only = cdc.compare(swift_ids, md_ids)
    assert (swift_only, md_only) == (set(), set()), (
        f"sources diverge:\n"
        f"  swift_only (in .swift, missing from .md): {sorted(swift_only)}\n"
        f"  md_only (in .md, missing from .swift):    {sorted(md_only)}"
    )


# --------------------------------------------------------------------------
# compare: drift detection in both directions
# --------------------------------------------------------------------------

def test_compare_detects_swift_only_id():
    """An extra ID in the swift set is reported in swift_only."""
    extra = "btn-fake-extra"
    synthetic_swift = _SWIFT_FILE.read_text() + (
        f'\n    public static let extraIdJustForThisTest = "{extra}"\n'
    )
    swift_ids = cdc.parse_swift_enum(synthetic_swift)
    md_ids = cdc.parse_md_reference(_MD_FILE.read_text())
    swift_only, md_only = cdc.compare(swift_ids, md_ids)
    assert extra in swift_only, (
        f"expected '{extra}' in swift_only, got swift_only={swift_only}"
    )
    assert extra not in md_only


def test_compare_detects_md_only_id():
    """An extra row in the markdown set is reported in md_only."""
    extra = "btn-fake-extra"
    synthetic_md = _MD_FILE.read_text() + f"\n| {extra} | extra label | button |\n"
    swift_ids = cdc.parse_swift_enum(_SWIFT_FILE.read_text())
    md_ids = cdc.parse_md_reference(synthetic_md)
    swift_only, md_only = cdc.compare(swift_ids, md_ids)
    assert extra in md_only, (
        f"expected '{extra}' in md_only, got md_only={md_only}"
    )
    assert extra not in swift_only


# --------------------------------------------------------------------------
# main() return codes
# --------------------------------------------------------------------------

def test_main_returns_0_on_match():
    """main() with no args defaults to the canonical repo paths and should exit 0."""
    rc = cdc.main([])
    assert rc == 0, f"expected main() to return 0 against the live tree, got {rc}"


def test_main_returns_1_on_synthetic_drift(tmp_path):
    """main() pointed at a drifted md file should exit 1."""
    extra_row = "| btn-fake-extra | extra | button |\n"
    tweaked_md = tmp_path / "ax-id-reference.md"
    tweaked_md.write_text(_MD_FILE.read_text() + extra_row)

    rc = cdc.main(["--md", str(tweaked_md)])
    assert rc == 1, (
        f"expected main() to return 1 when md has a spurious row, got {rc}"
    )
