"""Catches drift between AccessibilityIDs.swift and docs/ax-id-reference.md.

The two sources of truth for the project's AccessibilityID constants are:

  1. `phase3/FilmScanner/Sources/ScanlightApp/AccessibilityIDs.swift`
     Parsed via regex over `public static let <name> = "<value>"` lines.

  2. `docs/ax-id-reference.md`
     Parsed via regex over pipe-delimited table rows whose left cell starts
     with a known AX-ID prefix (btn-, field-, lbl-, slider-, scroll-, picker-,
     stepper-, toggle-).

This script asserts that the two sets of AX-ID strings are identical.

Exit codes:
  0 — Sources match. Prints "OK: N AX-IDs match." to stdout.
  1 — Sources diverge. Prints a diff block to stderr.

Usage:
  python3 scripts/check_docs_consistency.py
  python3 scripts/check_docs_consistency.py --swift <path> --md <path>

Relocation note: if Phase 02 lands `tests/integration/` later, this file
can be moved there as `test_docs_consistency.py` with the pure functions
imported from the relocated module. The consistency check is logically a
test, not an operator script — the scripts/ location is a pragmatic
default while tests/integration/ doesn't exist yet.
"""

import argparse
import re
import sys
from pathlib import Path


# Phase 06 added picker-/stepper-/toggle- control kinds (Settings + Calibration
# views). Phase 07 added list- (the frame-status LazyVStack). parse_swift_enum
# is prefix-agnostic, so the md parser must recognize the same prefixes or the
# two sources falsely diverge.
_KNOWN_PREFIXES = (
    "btn-", "field-", "lbl-", "slider-", "scroll-",
    "picker-", "stepper-", "toggle-", "list-",
)
_DEFAULT_SWIFT = "phase3/FilmScanner/Sources/ScanlightApp/AccessibilityIDs.swift"
_DEFAULT_MD = "docs/ax-id-reference.md"


def parse_swift_enum(swift_text: str) -> set[str]:
    """Parse `public static let <name> = "<value>"` lines and return the set of
    <value> strings.

    Excludes `schemaVersion` because its value ("1") is a schema marker, not
    an accessibility identifier. All other public-static-let string constants
    in the enum are AX-IDs.

    Regex: r'public static let\\s+(\\w+)\\s*=\\s*"([^"]+)"'
    """
    pattern = re.compile(r'public static let\s+(\w+)\s*=\s*"([^"]+)"')
    result = set()
    for name, value in pattern.findall(swift_text):
        if name == "schemaVersion":
            continue
        result.add(value)
    return result


def parse_md_reference(md_text: str) -> set[str]:
    """Parse pipe-delimited table rows from docs/ax-id-reference.md and return
    the set of left-column cell contents.

    Matches rows whose first cell starts with one of the known AX-ID prefixes:
    btn-, field-, lbl-, slider-, scroll-, picker-, stepper-, toggle-.

    Regex: r'^\\|\\s*([a-z][a-z0-9-]+)\\s*\\|' (anchored at line start after strip).
    """
    pattern = re.compile(r'^\|\s*([a-z][a-z0-9-]+)\s*\|')
    result = set()
    for line in md_text.splitlines():
        m = pattern.match(line.strip())
        if m:
            candidate = m.group(1)
            if any(candidate.startswith(p) for p in _KNOWN_PREFIXES):
                result.add(candidate)
    return result


def compare(swift_ids: set[str], md_ids: set[str]) -> tuple[set[str], set[str]]:
    """Return (swift_only, md_only). Empty pair means consistent.

    swift_only: IDs in AccessibilityIDs.swift but missing from ax-id-reference.md
    md_only:    IDs in ax-id-reference.md but missing from AccessibilityIDs.swift
    """
    return (swift_ids - md_ids, md_ids - swift_ids)


def main(argv: list[str] | None = None) -> int:
    """Parse both sources, compare, exit 0 on match or 1 on divergence.

    Argparse:
      --swift PATH  Path to AccessibilityIDs.swift (default: repo-relative)
      --md PATH     Path to ax-id-reference.md (default: repo-relative)

    Paths are resolved relative to the repo root when relative. The repo root
    is computed as the directory two levels above this script file, so the
    script works from any CWD.
    """
    p = argparse.ArgumentParser(
        prog="check_docs_consistency",
        description=(
            "Assert that AccessibilityIDs.swift and docs/ax-id-reference.md "
            "contain the same set of AX-ID strings."
        ),
    )
    p.add_argument(
        "--swift",
        default=_DEFAULT_SWIFT,
        help=f"Path to AccessibilityIDs.swift (default: {_DEFAULT_SWIFT})",
    )
    p.add_argument(
        "--md",
        default=_DEFAULT_MD,
        help=f"Path to ax-id-reference.md (default: {_DEFAULT_MD})",
    )
    args = p.parse_args(argv)

    # Resolve paths relative to repo root when not absolute.
    repo_root = Path(__file__).resolve().parent.parent
    swift_path = Path(args.swift)
    md_path = Path(args.md)
    if not swift_path.is_absolute():
        swift_path = repo_root / swift_path
    if not md_path.is_absolute():
        md_path = repo_root / md_path

    # Read both files — errors='strict' so a malformed file fails loudly.
    try:
        swift_text = swift_path.read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        print(
            f"check_docs_consistency: AccessibilityIDs.swift not found at {swift_path}",
            file=sys.stderr,
        )
        return 1

    try:
        md_text = md_path.read_text(encoding="utf-8", errors="strict")
    except FileNotFoundError:
        print(
            f"check_docs_consistency: ax-id-reference.md not found at {md_path}",
            file=sys.stderr,
        )
        return 1

    swift_ids = parse_swift_enum(swift_text)
    md_ids = parse_md_reference(md_text)
    swift_only, md_only = compare(swift_ids, md_ids)

    if not swift_only and not md_only:
        print(f"OK: {len(swift_ids)} AX-IDs match.")
        return 0

    print("check_docs_consistency: drift detected", file=sys.stderr)
    for id_str in sorted(swift_only):
        print(
            f"  + {id_str}  (in AccessibilityIDs.swift, missing from docs/ax-id-reference.md)",
            file=sys.stderr,
        )
    for id_str in sorted(md_only):
        print(
            f"  - {id_str}  (in docs/ax-id-reference.md, missing from AccessibilityIDs.swift)",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
