"""Render every formula in /formulas to /formulas.yaml (LaTeX + symbol map).

Runs the same shared override translation and rendering pipeline as build_seed
and latex, so the YAML output here is consistent with what the seed
migration would emit for the same input.

Exits non-zero on the first broken yaml. Fails fast: silent drift between
this batch renderer and the seed migration is exactly the class of bug
this script exists to prevent.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_SCIFIND = "/home/admin/Projects/Scifind"

for p in (str(_SCIFIND), str(_HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from scifind_lib import localise, open_database, parse_and_preview_equation  # noqa: E402,F401

from yaml_overrides import build_overrides  # noqa: E402

FORMULAS_DIR = _REPO / "formulas"
OUT_PATH = _REPO / "formulas.yaml"
LOCALE = "en-us"


def _symbol_map(conn, variables, tokens):
    """Build `{rendered_sym: display_name}` from a parsed-and-previewed equation.

    Constants are added first, then quantities, so a constant's symbol/name
    takes precedence on collision. When two quantities collide on the
    rendered symbol, the first occurrence wins (first-write-wins).
    """
    out = {}
    for tok in tokens:
        if tok.get("token_kind") != "constant":
            continue
        cid = tok.get("constant_id")
        if not cid:
            continue
        row = conn.execute(
            "SELECT id, name, symbol FROM constant WHERE id = ?", (cid,)
        ).fetchone()
        if not row:
            continue
        row = dict(row)
        sym = row.get("symbol") or row["id"]
        if not sym or sym in out:
            continue
        out[sym] = localise(row["name"] or {}, LOCALE)
    for v in variables:
        sym = v.get("symbol_overwrite") or v.get("symbol") or v["id"]
        if not sym or sym in out:
            continue
        if not str(sym).strip():
            continue
        default_name = localise(v.get("name") or {}, LOCALE)
        name_ov = v.get("name_overwrite") or ""
        out[sym] = f"{name_ov} ({default_name})" if name_ov else default_name
    return out


def main():
    conn = open_database()
    try:
        records = _render_all(conn)
    finally:
        conn.close()
    _write_output(records)


def _render_all(conn) -> dict:
    records: dict = {}
    for p in sorted(FORMULAS_DIR.glob("*.yaml")):
        with open(p) as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            print(f"error: {p.name}: top-level YAML is not a mapping", file=sys.stderr)
            sys.exit(1)
        yaml_id = (data.get("id") or "").strip()
        if not yaml_id:
            print(f"error: {p.name}: missing `id:` field", file=sys.stderr)
            sys.exit(1)
        if yaml_id != p.stem:
            print(
                f"error: {p.name}: yaml id {yaml_id!r} does not match "
                f"filename {p.stem!r}",
                file=sys.stderr,
            )
            sys.exit(1)
        eq = (data.get("equation") or "").strip()
        if not eq:
            print(f"error: {p.name}: missing `equation` field", file=sys.stderr)
            sys.exit(1)

        overrides = build_overrides(
            conn, eq, data.get("symbol_overrides"), data.get("name_overrides")
        )
        result = parse_and_preview_equation(
            conn, eq, locale=LOCALE, overrides=overrides
        )
        err = result.get("error")
        if err:
            print(f"error: {p.name}: {err}", file=sys.stderr)
            sys.exit(1)

        records[yaml_id] = {
            "latex": result["latex"],
            "symbols": _symbol_map(conn, result["variables"], result["tokens"]),
        }
    return records


def _write_output(records: dict) -> None:
    # Atomic write: dump to a sibling temp file and rename, so a crash
    # mid-write can't leave a truncated formulas.yaml behind.
    tmp = OUT_PATH.with_suffix(OUT_PATH.suffix + ".tmp")
    with open(tmp, "w") as out:
        yaml.dump(records, out, default_flow_style=False, allow_unicode=True, sort_keys=False)
    tmp.replace(OUT_PATH)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
