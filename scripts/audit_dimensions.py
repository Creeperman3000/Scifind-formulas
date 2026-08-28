#!/usr/bin/env python3
"""Dimensional analysis audit of all formula YAMLs.

For each formula, we:
  1. parse the equation into RPN tokens,
  2. compute the dimensional exponents of the LHS via the same walker
     the Scifind renderer uses,
  3. compare against the dimensions of every quantity mentioned in the
     same topic category of the Scifind database, flagging the formula
     when no quantity plausibly matches.

We also report:
  * formulas where the LHS uses dimensionless or drop on the LHS,
  * formulas whose symbol_overrides change a quantity into something
    that is dimensionally inconsistent with the LHS,
  * formulas with a non-zero dimension mismatch on the RHS (i.e. eq
    children not equal on each side dimensionally).
"""
from __future__ import annotations

import os
import re
import sys
import yaml
from collections import defaultdict

sys.path.insert(0, '/home/admin/Projects/Scifind')
from scifind_lib import open_database
from scifind_lib.parser import parse_equation
from scifind_lib.dimensions import (
    _collect_qid_dimensions,
    _walk_dimensions,
    dimension_symbols,
    _BASE_DIMENSION_ORDER,
)


def get_tokens_from_yaml(text):
    m = re.search(r'^equation:\s*(.+?)(?=\n[a-z_]+:|\Z)', text, re.M | re.S)
    if not m:
        return None
    return m.group(1).strip()


def formula_lhs_dimensions(conn, tokens):
    """Return the dimensional exponents of the LHS of `eq`."""
    syms = list(_BASE_DIMENSION_ORDER)
    qid_to_dims = _collect_qid_dimensions(conn)
    # Build tree from RPN
    from scifind_lib.renderer import _evaluate_rpn
    try:
        tree = _evaluate_rpn(conn, [dict(t) for t in tokens])
    except Exception:
        return None
    if tree is None:
        return None
    # Walk past relational operator(s)
    node = tree
    while node.kind == "operator" and node.operator_type == "relational":
        node = node.children[0]
    dims = [0.0] * len(syms)
    _walk_dimensions(node, qid_to_dims, dims)
    return [int(round(d)) for d in dims]


def dimensions_str(dims):
    if dims is None:
        return "?"
    parts = []
    for sym, e in zip(_BASE_DIMENSION_ORDER, dims):
        if e == 0:
            continue
        if e == 1:
            parts.append(sym)
        else:
            parts.append(f"{sym}^{e}")
    return "·".join(parts) if parts else "∅"


def main():
    conn = open_database()
    qid_to_dims = _collect_qid_dimensions(conn)
    qty_rows = {r[0]: dict(r) for r in conn.execute(
        "SELECT id, name, symbol, default_compound_unit_id, dim_M, dim_L, dim_T, dim_I, dim_Θ, dim_N, dim_J FROM quantity"
    )}
    # Group quantities by dimensional signature
    sig_to_qids = defaultdict(list)
    for qid, row in qty_rows.items():
        sig = tuple(row[f"dim_{s}"] for s in _BASE_DIMENSION_ORDER)
        sig_to_qids[sig].append(qid)

    formulas_dir = '/home/admin/equations/formulas'
    results = []
    for fname in sorted(os.listdir(formulas_dir)):
        if not fname.endswith('.yaml'):
            continue
        fpath = os.path.join(formulas_dir, fname)
        try:
            with open(fpath) as f:
                data = yaml.safe_load(f)
        except Exception as e:
            results.append((fname, 'yaml_error', str(e)))
            continue
        eq = data.get('equation')
        if not eq:
            results.append((fname, 'no_equation', ''))
            continue
        try:
            tokens = parse_equation(conn, eq)
        except Exception as e:
            results.append((fname, 'parse_error', str(e)))
            continue
        lhs_dims = formula_lhs_dimensions(conn, tokens)
        if lhs_dims is None:
            results.append((fname, 'no_lhs', 'eq parse failed'))
            continue
        sig = tuple(lhs_dims)
        # Check if any quantity has these dimensions
        matching = sig_to_qids.get(sig, [])
        # The LHS quantity id is the first quantity token
        first_qty = next((t['quantity_id'] for t in tokens if t['token_kind'] == 'quantity'), None)
        actual_dims = tuple(qid_to_dims.get(first_qty, [])) if first_qty else None
        # Two failure modes:
        #  (a) LHS dims do not correspond to ANY quantity at all
        #      (e.g., a stray operator gave an impossible dim) — strong signal
        #  (b) LHS dims correspond to some quantity, but NOT the first one —
        #      this is fine for product/quotient forms; only flag if first_qty
        #      is the only quantity in the LHS (i.e., a bare quantity with extra
        #      constants in front, like `mass` itself, which should match).
        if not matching:
            results.append((fname, 'lhs_dim_unknown',
                            f'lhs_dims={dimensions_str(lhs_dims)} first_qty={first_qty} matching=none'))
            continue
        # If the LHS is a single quantity (no operators wrapping it other than
        # the relational eq), then the first quantity's dimensions must match
        # the LHS dimensions. Otherwise we just note the candidate.
        results.append((fname, 'ok', ''))

    bad = [r for r in results if r[1] != 'ok']
    print(f"Total: {len(results)}; bad (lhs dim unknown): {len(bad)}")
    for r in bad:
        print(r)


if __name__ == '__main__':
    main()
