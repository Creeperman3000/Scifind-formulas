import glob
import os
import sys

import yaml

sys.path.insert(0, "/home/admin/Projects/Scifind")
from scifind_lib import localise, open_database, parse_equation, preview_equation

FORMULAS_DIR = "/home/admin/equations/formulas"
OUT_PATH = "/home/admin/equations/formulas.yaml"
LOCALE = "en-us"


def _build_overrides(conn, equation, symbol_overrides, name_overrides):
    """Map the YAML's per-occurrence override arrays onto the
    `quantity_id|label|abs_pos` keys that scifind_lib.preview_equation expects.

    The YAML stores overrides as a flat array of {quantity_id: value} dicts in
    equation order, where the i-th entry applies to the i-th *quantity-like*
    token — including `skip` sentinels. The original render_latex counted
    skip tokens in its qty_pos counter, and the YAML files were authored to
    align with that count (e.g. a sum with `skip skip` reserves two slots in
    the override list). preview_equation instead keys overrides by absolute
    token position, so we walk the token stream once to translate the indices.
    """
    tokens = parse_equation(conn, equation)
    sym_iter = iter(symbol_overrides or [])
    name_iter = iter(name_overrides or [])
    overrides = {}
    for abs_pos, tok in enumerate(tokens, start=1):
        if tok["token_kind"] != "quantity":
            continue
        sym_entry = next(sym_iter, None) or {}
        name_entry = next(name_iter, None) or {}
        # The original schema encodes a per-occurrence override as a
        # {quantity_id: value} dict. We ignore the dict's quantity_id key
        # (it's documentation, not a constraint) and apply the value to this
        # token regardless. `skip` tokens are included in the YAML ordering
        # so a `sum ... skip skip ...` reserves slots for the bounds.
        sym_val = next(iter(sym_entry.values()), None) if sym_entry else None
        name_val = next(iter(name_entry.values()), None) if name_entry else None
        if sym_val is None and name_val is None:
            continue
        # For `skip` tokens we still need an override entry (the YAML has one
        # for each skip slot), but the rendered output is governed by the
        # surrounding operator (sum/int/etc.) — the key just needs to be
        # unique and addressable. preview_equation won't apply these to a
        # skip node's symbol, so we tag them with the skip quantity_id.
        qid = tok["quantity_id"]
        label = tok.get("label") or ""
        key = f"{qid}|{label}|{abs_pos}"
        overrides[key] = {
            "symbol": str(sym_val) if sym_val else "",
            "name": str(name_val) if name_val else "",
        }
    return overrides


def _symbol_map(conn, variables, tokens):
    """Mirror the original output's `symbols: {rendered_sym: display_name}`.

    Constants are added first, then quantities, so a constant's symbol/name
    takes precedence on collision (the original AST walk visited constants
    before user-overridden quantities). When two quantities collide on the
    rendered symbol, the first occurrence wins — also matching the old
    first-write-wins dedup.

    Quantities come from preview_equation's `variables` (already keyed and
    overridden). Constants are pulled from the RPN token stream because the
    public API doesn't surface them as variables, and the original output's
    symbol map included them (e.g. g_0, h, c, e, k_B, N_A, \\mu_0,
    \\varepsilon_0, R, \\pi, etc.).
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
        # Quantities with no symbol (e.g. `dimensionless`, `skip`) render
        # nothing in the LaTeX; they shouldn't pollute the symbol map with
        # the bare id as a key.
        if not str(sym).strip():
            continue
        default_name = localise(v.get("name") or {}, LOCALE)
        name_ov = v.get("name_overwrite") or ""
        out[sym] = f"{name_ov} ({default_name})" if name_ov else default_name
    return out


def main():
    conn = open_database()
    records = {}
    for fpath in sorted(glob.glob(os.path.join(FORMULAS_DIR, "*.yaml"))):
        with open(fpath) as f:
            data = yaml.safe_load(f)
        name = data.get("name", os.path.basename(fpath))
        eq = data.get("equation", "")
        try:
            overrides = _build_overrides(conn, eq, data.get("symbol_overrides"), data.get("name_overrides"))
            result = preview_equation(conn, eq, locale=LOCALE, overrides=overrides)
            if result.get("error"):
                records[name] = {"latex": f"ERROR: {result['error']}", "symbols": {}}
            else:
                records[name] = {
                    "latex": result["latex"],
                    "symbols": _symbol_map(conn, result["variables"], result["tokens"]),
                }
        except Exception as e:
            records[name] = {"latex": f"ERROR: {e}", "symbols": {}}

    with open(OUT_PATH, "w") as out:
        yaml.dump(records, out, default_flow_style=False, allow_unicode=True, sort_keys=False)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
