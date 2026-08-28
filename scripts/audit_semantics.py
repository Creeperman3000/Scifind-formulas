#!/usr/bin/env python3
"""Comprehensive semantic audit of all formula YAMLs.

Goes beyond the basic schema audit and identifies:
  1. Formulas whose LHS dimensions don't correspond to any quantity in the
     database (suggesting a missing quantity, a wrong LHS, or a wrong
     formula).
  2. Formulas whose RHS children have inconsistent dimensions across an `eq`.
  3. Formulas that use `dimensionless` as a LHS or RHS that should
     actually carry a real quantity's symbol (e.g. m for an integer).
  4. Formulas that use `drop` for a quantity that should have a defined
     identity (e.g., "no quantity on LHS" -> maybe the formula should use
     a different root like `approx` or a definition).
  5. Formulas where the equation references a constant / quantity that
     doesn't exist in the database.
  6. Suspicious overrides: symbol_overrides that change a quantity to a
     symbol that looks like a snake_case identifier (per audit guidelines).

The output is grouped by category so the user can review and act on
each issue.
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
    _BASE_DIMENSION_ORDER,
)


def dim_str(dims):
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
    sig_to_qids = defaultdict(list)
    for qid, row in qty_rows.items():
        sig = tuple(row[f"dim_{s}"] for s in _BASE_DIMENSION_ORDER)
        sig_to_qids[sig].append(qid)

    formulas_dir = '/home/admin/equations/formulas'
    findings = defaultdict(list)  # category -> list of (fname, detail)

    for fname in sorted(os.listdir(formulas_dir)):
        if not fname.endswith('.yaml'):
            continue
        fpath = os.path.join(formulas_dir, fname)
        try:
            with open(fpath) as f:
                data = yaml.safe_load(f)
        except Exception as e:
            findings['yaml_error'].append((fname, str(e)))
            continue
        eq = data.get('equation')
        if not eq:
            findings['no_equation'].append((fname, ''))
            continue
        # Tokenize
        try:
            tokens = parse_equation(conn, eq)
        except Exception as e:
            findings['parse_error'].append((fname, str(e)))
            continue
        # Build tree
        from scifind_lib.renderer import _evaluate_rpn
        try:
            tree = _evaluate_rpn(conn, [dict(t) for t in tokens])
        except Exception as e:
            findings['rpn_reduction_error'].append((fname, str(e)))
            continue
        if tree is None:
            findings['empty_tree'].append((fname, ''))
            continue

        # ── LHS dimensional analysis ──
        lhs_node = tree
        while lhs_node.kind == "operator" and lhs_node.operator_type == "relational":
            lhs_node = lhs_node.children[0]
        lhs_dims = [0.0] * len(_BASE_DIMENSION_ORDER)
        _walk_dimensions(lhs_node, qid_to_dims, lhs_dims)
        lhs_dims = [int(round(d)) for d in lhs_dims]
        sig = tuple(lhs_dims)
        matching = sig_to_qids.get(sig, [])
        first_qty = next((t['quantity_id'] for t in tokens if t['token_kind'] == 'quantity' and t['quantity_id'] != 'drop'), None)

        if not matching:
            findings['lhs_no_matching_qty'].append((fname,
                f'lhs_dims={dim_str(lhs_dims)} first_qty={first_qty}'))

        # ── RHS/RHS dimensional mismatch across eq ──
        if tree.kind == "operator" and tree.operator_type == "relational" and tree.arity >= 2:
            rhs_nodes = tree.children[1:]
            rhs_dims_list = []
            for rn in rhs_nodes:
                rd = [0.0] * len(_BASE_DIMENSION_ORDER)
                _walk_dimensions(rn, qid_to_dims, rd)
                rhs_dims_list.append(tuple(int(round(d)) for d in rd))
            if len(set(rhs_dims_list)) > 1:
                findings['rhs_inconsistent_dims'].append((fname,
                    f'lhs={dim_str(lhs_dims)} rhs={", ".join(dim_str(d) for d in rhs_dims_list)}'))

        # ── snake_case symbol_overrides ──
        for ovr in data.get('symbol_overrides') or []:
            if not isinstance(ovr, dict):
                continue
            for qid, sym in ovr.items():
                if not isinstance(sym, str):
                    continue
                # Heuristics: contains `_` with more than 2 chars before/after,
                # i.e. looks like a Python identifier, not a subscript.
                # Single-letter subscripts (m_e, v_x) are fine; multi-word
                # subscripts (mass_electron) are not.
                # Also reject if contains a `_` then 2+ letters then `_` again.
                if re.search(r'[a-z][a-z]+_[a-z][a-z]+', sym):
                    findings['snake_case_symbol'].append((fname, f'{qid} -> {sym!r}'))

        # ── `dimensionless` on LHS of a meaningful equation ──
        first_tok = tokens[0] if tokens else None
        if first_tok and first_tok.get('token_kind') == 'quantity' and first_tok.get('quantity_id') == 'dimensionless':
            findings['dimensionless_lhs'].append((fname, eq[:80]))

        # ── `drop` on LHS ──
        if first_tok and first_tok.get('token_kind') == 'quantity' and first_tok.get('quantity_id') == 'drop':
            findings['drop_lhs'].append((fname, eq[:80]))

    for cat, items in findings.items():
        print(f"\n{'='*70}\n{cat}: {len(items)}\n{'='*70}")
        for f, d in items:
            print(f"  {f}: {d}")


if __name__ == '__main__':
    main()
