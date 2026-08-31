#!/usr/bin/env python3
"""Render a formula's LaTeX by id.

Usage:
    python script/latex.py <formula_id>

Looks up <formulas>/<formula_id>.yaml, applies its symbol/name overrides, and
prints the rendered LaTeX to stdout.

Exits non-zero on the first error (missing file, unparseable yaml, missing
equation, parse failure, missing id). Fails fast rather than emitting
"ERROR: ..." rows: silent drift between this renderer and the seed migration
is exactly the class of bug this script exists to prevent.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_SCIFIND = "/home/admin/Projects/Scifind"

for p in (str(_SCIFIND), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from scifind_lib import open_database, parse_and_preview_equation  # noqa: E402

from yaml_overrides import build_overrides  # noqa: E402

FORMULAS_DIR = _REPO / "formulas"


def main():
    parser = argparse.ArgumentParser(description="Render a formula's LaTeX by id.")
    parser.add_argument("formula_id", help="Formula id (matches the YAML filename stem).")
    args = parser.parse_args()

    yaml_path = FORMULAS_DIR / f"{args.formula_id}.yaml"
    if not yaml_path.is_file():
        print(f"error: formula not found: {yaml_path}", file=sys.stderr)
        sys.exit(1)

    with open(yaml_path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        print(f"error: {yaml_path} did not parse as a mapping", file=sys.stderr)
        sys.exit(1)

    eq = (data.get("equation") or "").strip()
    if not eq:
        print(f"error: {yaml_path} has no `equation` field", file=sys.stderr)
        sys.exit(1)
    yaml_id = (data.get("id") or "").strip()
    if not yaml_id:
        print(f"error: {yaml_path} has no `id:` field", file=sys.stderr)
        sys.exit(1)
    if yaml_id != args.formula_id:
        print(
            f"error: {yaml_path} id {yaml_id!r} does not match requested "
            f"id {args.formula_id!r}",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = open_database()
    try:
        overrides = build_overrides(
            conn, eq, data.get("symbol_overrides"), data.get("name_overrides")
        )
        result = parse_and_preview_equation(conn, eq, overrides=overrides)
    finally:
        conn.close()

    err = result.get("error")
    if err:
        print(f"error: {err}", file=sys.stderr)
        sys.exit(1)

    print(result["latex"])


if __name__ == "__main__":
    main()
