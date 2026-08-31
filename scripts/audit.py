#!/usr/bin/env python3
"""Unified audit of every formula YAML against the Scifind schema and pipeline.

This replaces the previous scattered audit scripts (audit_lint, audit_malformed,
audit_dimensions, audit_schema, audit_scifind, audit_semantics, dedupe_find,
dedupe_names, dedupe_report, find_dim_errors, find_struct_errors) with a single
tool whose checks are clean, independent functions.

Checks (select with --check NAME; default runs every check):

  yaml_schema   YAML parses; id present and matches filename; topic is a leaf
                of tree.yaml; equation present; difficulty in [1,10].
  overrides     symbol_overrides / name_overrides length matches the number of
                quantity tokens; no override references a quantity absent from
                the equation.
  refs          every quantity / constant / operator the equation references
                exists in the scifind.db vocabulary.
  dim_lhs       the LHS dimensions correspond to at least one quantity in the
                database (a strong signal the LHS is plausible).
  dim_rhs       all RHS sides of a relational root have mutually consistent
                dimensions.
  dim_strict    full dimension walk that fails fast on the first mismatch in
                each formula (catches the mismatches the renderer swallows).
  render        parses and renders cleanly; no snake_case leaked into symbols;
                no malformed latex (pi2, theta2, dimensionless^2, ...).
  semantic      LHS root is not `dimensionless` / `drop`; no snake_case-only
                symbol overrides.
  duplicates    formulas duplicated by exact equation, by normalized tree, or
                by yaml `name`.

Exit codes:
  0   all requested checks passed (no findings at or above the severity floor)
  1   at least one finding at or above the severity floor
  2   the script itself crashed (bad args, missing file, database failure)

Output is human-readable by default. Pass --json to emit a machine-readable
finding list, and --severity to raise/lower the gate (default "info").
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_SCIFIND = Path("/home/admin/Projects/Scifind")

for _p in (str(_SCIFIND), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from scifind_lib import localise, open_database, parse_and_preview_equation  # noqa: E402
from scifind_lib.dimensions import (  # noqa: E402
    DimensionMismatchError,
    _BASE_DIMENSION_ORDER,
    _collect_qid_dimensions,
    _compute_dimensions,
    _walk_dimensions,
    dimension_columns,
)
from scifind_lib.parser import parse_equation  # noqa: E402
from scifind_lib.renderer import reduce_rpn_to_tree  # noqa: E402

from yaml_overrides import build_overrides  # noqa: E402

FORMULAS_DIR = _REPO / "formulas"
TOPIC_YAML = _REPO / "tree.yaml"
LOCALE = "en-us"

_SEVERITY_ORDER = {"info": 0, "warning": 1, "error": 2}


# ── Finding model ──────────────────────────────────────────────────────────

@dataclass
class Finding:
    """A single problem found for one formula (or the dataset as a whole)."""

    check: str
    formula_id: str = ""
    severity: str = "warning"
    message: str = ""
    context: dict = field(default_factory=dict)


# ── Shared helpers ─────────────────────────────────────────────────────────

def load_formulas() -> list[tuple[Path, dict]]:
    """Load every *.yaml under FORMULAS_DIR as (path, data).

    Files that fail to parse return (path, None); the yaml_schema check turns
    that into a finding. Sorted for stable, reproducible output.
    """
    out: list[tuple[Path, dict]] = []
    for p in sorted(FORMULAS_DIR.glob("*.yaml")):
        try:
            data = yaml.safe_load(p.read_text())
        except Exception as e:  # noqa: BLE001
            out.append((p, {"_yaml_error": str(e)}))
            continue
        if not isinstance(data, dict):
            out.append((p, {"_yaml_error": "top-level YAML is not a mapping"}))
            continue
        out.append((p, data))
    return out


def tokenize(conn, equation: str):
    """Return parsed RPN tokens, or None if parsing fails."""
    try:
        return parse_equation(conn, equation)
    except (ValueError, KeyError, IndexError, TypeError, sqlite3.Error):
        return None


def reduce(conn, tokens):
    """Reduce RPN tokens to an expression tree, or None on reducible failure.

    Catches only the errors the renderer itself tolerates; lets real bugs
    (NameError, AttributeError, KeyError on missing fields) propagate so the
    outer driver reports a crash instead of silently masking problems.
    """
    try:
        return reduce_rpn_to_tree(conn, [dict(t) for t in tokens])
    except (ValueError, TypeError, IndexError, sqlite3.Error):
        return None


def leaf_topics() -> set[str]:
    """Extract leaf topic ids from tree.yaml (a leaf has no more-indented child).

    tree.yaml is an indented list; PyYAML flattens branches to strings, so we
    parse indentation manually. A topic is a leaf iff no subsequent entry has a
    strictly greater indent than it.
    """
    raw = [
        l for l in TOPIC_YAML.read_text().split("\n") if l.lstrip().startswith("-")
    ]
    items = []
    for line in raw:
        indent = len(line) - len(line.lstrip())
        items.append((indent, line.lstrip()[1:].strip()))
    leaves = set()
    for i, (indent, _) in enumerate(items):
        has_deeper = any(items[j][0] > indent for j in range(i + 1, len(items)))
        if not has_deeper:
            leaves.add(items[i][1])
    return leaves


def db_vocab(conn) -> tuple[set, set, set]:
    """Return (quantities, constants, operators) ids present in the DB."""
    q = {r[0] for r in conn.execute("SELECT id FROM quantity")}
    c = {r[0] for r in conn.execute("SELECT id FROM constant")}
    o = {r[0] for r in conn.execute("SELECT id FROM operator")}
    return q, c, o


def quantity_signatures(conn):
    """Map a dimensional signature tuple -> list of quantity ids with it."""
    cols = dimension_columns()
    sig_to_qids = defaultdict(list)
    for row in conn.execute(
        f"SELECT id, {', '.join(cols)} FROM quantity"
    ):
        d = dict(row)
        sig = tuple(d[c] for c in cols)
        sig_to_qids[sig].append(d["id"])
    return sig_to_qids


# ── Individual checks ──────────────────────────────────────────────────────

def check_yaml_schema(conn, formulas) -> list[Finding]:
    """Structural integrity: parse, id/filename, topic, equation, difficulty."""
    valid_topics = leaf_topics()
    findings = []
    for path, data in formulas:
        fid = path.stem

        if "_yaml_error" in data:
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="error",
                message=data["_yaml_error"],
                context={"file": path.name},
            ))
            continue

        yaml_id = (data.get("id") or "").strip()
        if not yaml_id:
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="error",
                message="missing `id:` field", context={"file": path.name},
            ))
        elif yaml_id != fid:
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="error",
                message=f"id {yaml_id!r} does not match filename {fid!r}",
                context={"file": path.name},
            ))

        if not (data.get("name") or "").strip():
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="error",
                message="missing `name:` field", context={"file": path.name},
            ))

        topic = data.get("topic")
        if not topic:
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="error",
                message="missing `topic:` field", context={"file": path.name},
            ))
        elif topic not in valid_topics:
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="warning",
                message=f"topic {topic!r} is not a leaf topic in tree.yaml",
                context={"file": path.name},
            ))

        eq = (data.get("equation") or "").strip()
        if not eq:
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="error",
                message="missing `equation:` field", context={"file": path.name},
            ))

        diff = data.get("difficulty")
        if diff is None:
            findings.append(Finding(
                check="yaml_schema", formula_id=fid, severity="error",
                message="missing `difficulty` field", context={"file": path.name},
            ))
        else:
            try:
                dval = int(diff)
                if dval < 1 or dval > 10:
                    findings.append(Finding(
                        check="yaml_schema", formula_id=fid, severity="warning",
                        message=f"difficulty {diff!r} is outside [1,10]",
                        context={"file": path.name},
                    ))
            except (ValueError, TypeError):
                findings.append(Finding(
                    check="yaml_schema", formula_id=fid, severity="warning",
                    message=f"difficulty {diff!r} is not an integer",
                    context={"file": path.name},
                ))
    return findings


def check_overrides(conn, formulas) -> list[Finding]:
    """symbol/name override lists must line up with quantity-token occurrences."""
    findings = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        if not eq:
            continue
        tokens = tokenize(conn, eq)
        if tokens is None:
            continue
        qty_tokens = [t for t in tokens if t.get("token_kind") == "quantity"]
        num_qty = len(qty_tokens)

        for field_name in ("symbol_overrides", "name_overrides"):
            ov = data.get(field_name) or []
            if not isinstance(ov, list):
                findings.append(Finding(
                    check="overrides", formula_id=fid, severity="warning",
                    message=f"{field_name} is not a list",
                    context={"file": path.name},
                ))
                continue
            if len(ov) != num_qty:
                findings.append(Finding(
                    check="overrides", formula_id=fid, severity="error",
                    message=f"{field_name} count {len(ov)} != quantity tokens {num_qty}",
                    context={"file": path.name},
                ))

        # Phantom quantity ids referenced by overrides but absent from equation.
        actual_qids = {t.get("quantity_id") for t in qty_tokens}
        for field_name in ("symbol_overrides", "name_overrides"):
            referenced = set()
            for entry in data.get(field_name) or []:
                if isinstance(entry, dict):
                    referenced.update(k for k in entry if k)
            phantom = referenced - actual_qids
            if phantom:
                findings.append(Finding(
                    check="overrides", formula_id=fid, severity="warning",
                    message=f"{field_name} references qids absent from equation: "
                            f"{sorted(phantom)}",
                    context={"file": path.name},
                ))
    return findings


def check_refs(conn, formulas) -> list[Finding]:
    """Every quantity / constant / operator must exist in the DB."""
    quantities, constants, operators = db_vocab(conn)
    findings = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        if not eq:
            continue
        tokens = tokenize(conn, eq)
        if tokens is None:
            continue
        for tok in tokens:
            kind = tok.get("token_kind")
            if kind == "quantity":
                qid = tok.get("quantity_id")
                if qid and qid not in quantities:
                    findings.append(Finding(
                        check="refs", formula_id=fid, severity="error",
                        message=f"unknown quantity {qid!r}", context={"file": path.name},
                    ))
            elif kind == "constant":
                cid = tok.get("constant_id")
                if cid and cid not in constants:
                    findings.append(Finding(
                        check="refs", formula_id=fid, severity="error",
                        message=f"unknown constant {cid!r}", context={"file": path.name},
                    ))
            elif kind == "operator":
                oid = tok.get("operator_id")
                if oid and oid not in operators:
                    findings.append(Finding(
                        check="refs", formula_id=fid, severity="error",
                        message=f"unknown operator {oid!r}", context={"file": path.name},
                    ))
    return findings


def _dim_str(dims) -> str:
    if dims is None:
        return "?"
    parts = []
    for sym, e in zip(_BASE_DIMENSION_ORDER, dims):
        if e == 0:
            continue
        parts.append(sym if e == 1 else f"{sym}^{e}")
    return "·".join(parts) if parts else "∅"


def check_dim_lhs(conn, formulas) -> list[Finding]:
    """LHS dimensions must correspond to some quantity in the database."""
    qid_to_dims = _collect_qid_dimensions(conn)
    sig_to_qids = quantity_signatures(conn)
    findings = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        if not eq:
            continue
        tokens = tokenize(conn, eq)
        if tokens is None:
            continue
        tree = reduce(conn, tokens)
        if tree is None:
            continue
        node = tree
        while node.kind == "operator" and node.operator_type == "relational":
            node = node.children[0]
        dims = [0.0] * len(_BASE_DIMENSION_ORDER)
        try:
            _walk_dimensions(node, qid_to_dims, dims)
        except DimensionMismatchError:
            continue
        dims = tuple(int(round(d)) for d in dims)
        if dims not in sig_to_qids:
            first_qty = next(
                (t["quantity_id"] for t in tokens
                 if t.get("token_kind") == "quantity" and t.get("quantity_id") != "drop"),
                None,
            )
            findings.append(Finding(
                check="dim_lhs", formula_id=fid, severity="warning",
                message=(
                    f"LHS dimensions {_dim_str(list(dims))} match no database "
                    f"quantity" + (f"; first quantity {first_qty!r}" if first_qty else "")
                ),
                context={"file": path.name, "lhs_dims": _dim_str(list(dims))},
            ))
    return findings


def check_dim_rhs(conn, formulas) -> list[Finding]:
    """RHS sides of a relational root must be dimensionally consistent."""
    qid_to_dims = _collect_qid_dimensions(conn)
    findings = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        if not eq:
            continue
        tokens = tokenize(conn, eq)
        if tokens is None:
            continue
        tree = reduce(conn, tokens)
        if tree is None:
            continue
        if not (tree.kind == "operator" and tree.operator_type == "relational"):
            continue
        if tree.arity < 2:
            continue
        rhs_dims = []
        failed = False
        for rn in tree.children[1:]:
            rd = [0.0] * len(_BASE_DIMENSION_ORDER)
            try:
                _walk_dimensions(rn, qid_to_dims, rd)
            except DimensionMismatchError:
                failed = True
                break
            rhs_dims.append(tuple(int(round(d)) for d in rd))
        if failed:
            continue
        if len(set(rhs_dims)) > 1:
            findings.append(Finding(
                check="dim_rhs", formula_id=fid, severity="warning",
                message=f"inconsistent RHS dimensions: "
                        f"{', '.join(_dim_str(list(d)) for d in rhs_dims)}",
                context={"file": path.name,
                         "rhs_dims": [_dim_str(list(d)) for d in rhs_dims]},
            ))
    return findings


def check_dim_strict(conn, formulas) -> list[Finding]:
    """Full dimension walk, fail-fast per formula, mirroring renderer semantics.

    The renderer swallows DimensionMismatchError; this surfaces each one with
    its formula id so nothing silently degrades.
    """
    qid_to_dims = _collect_qid_dimensions(conn)
    findings = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        if not eq:
            continue
        tokens = tokenize(conn, eq)
        if tokens is None:
            findings.append(Finding(
                check="dim_strict", formula_id=fid, severity="error",
                message="equation failed to parse", context={"file": path.name},
            ))
            continue
        overrides = build_overrides(
            conn, eq, data.get("symbol_overrides"), data.get("name_overrides"),
        )
        tokens = [dict(t) for t in tokens]
        for pos, tok in enumerate(tokens, start=1):
            if tok["token_kind"] != "quantity":
                continue
            tok["pos"] = pos
            key = tok["quantity_id"] + "|" + (tok.get("label") or "") + "|" + str(pos)
            ov = overrides.get(key) or {}
            if ov.get("symbol"):
                tok["symbol_overwrite"] = ov["symbol"]
            if ov.get("name_overwrite"):
                tok["name_overwrite"] = ov["name_overwrite"]
        tree = reduce(conn, tokens)
        if tree is None:
            findings.append(Finding(
                check="dim_strict", formula_id=fid, severity="error",
                message="RPN tree failed to reduce", context={"file": path.name},
            ))
            continue
        try:
            _compute_dimensions(conn, tree, [0.0] * len(dimension_columns()))
        except DimensionMismatchError as e:
            findings.append(Finding(
                check="dim_strict", formula_id=fid, severity="warning",
                message=str(e), context={"file": path.name},
            ))
    return findings


# Malformed latex patterns from the old audit_malformed script.
_MALFORMED_PATTERNS = [
    (r"\\pi\s*2", "pi2"),
    (r"\\theta\s*2", "theta2"),
    (r"\\alpha\s*2", "alpha2"),
    (r"\\alpha\s*3", "alpha3"),
    (r"\\gamma\s*2", "gamma2"),
    (r"\\gamma\s*4", "gamma4"),
    (r"\\varepsilon\s*0", "varepsilon0"),
    (r"\\eta\s*2", "eta2"),
    (r"\\eta\s*3", "eta3"),
    (r"\\omega\s*2", "omega2"),
    (r"\\omega\s*0", "omega0"),
    (r"\\sigma\s*2", "sigma2"),
    (r"\\sigma\s*4", "sigma4"),
    (r"\\sigma\s*8", "sigma8"),
    (r"\\tau\s*2", "tau2"),
    (r"\\lambda\s*2", "lambda2"),
    (r"\\nu\s*2", "nu2"),
    (r"\\Delta\s*2", "Delta2"),
    (r"\\Delta\s*3", "Delta3"),
    (r"\\Delta\s*4", "Delta4"),
    (r"\\sum\s*2", "sum2"),
    (r"\\prod\s*2", "prod2"),
    (r"\\int\s*2", "int2"),
    (r"\\frac\s*2", "frac2"),
    (r"\\oint\s*2", "oint2"),
    (r"\\sqrt\s*2", "sqrt2"),
    (r"\\partial\s*2", "partial2"),
    (r"\\dot\s*2", "dot2"),
    (r"\\hat\s*2", "hat2"),
    (r"\\vec\s*2", "vec2"),
    (r"dimensionless\^?2", "dimensionless2"),
    (r"dimensionless\^?3", "dimensionless3"),
    (r"dimensionless\^?4", "dimensionless4"),
    (r"dimensionless\^?½", "dimensionless_1_2"),
    (r"dimensionless\^?⅓", "dimensionless_1_3"),
]
_BAD_SYMBOL_LITERALS = {"dimensionless", "dimension", "drop"}


def check_render(conn, formulas) -> list[Finding]:
    """Parse + render cleanly; flag snake_case leakage and malformed latex."""
    findings = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        if not eq:
            continue
        overrides = build_overrides(
            conn, eq, data.get("symbol_overrides"), data.get("name_overrides"),
        )
        try:
            result = parse_and_preview_equation(
                conn, eq, locale=LOCALE, overrides=overrides,
            )
        except Exception as e:  # noqa: BLE001
            findings.append(Finding(
                check="render", formula_id=fid, severity="error",
                message=f"render threw {type(e).__name__}: {e}",
                context={"file": path.name},
            ))
            continue
        err = result.get("error")
        if err:
            findings.append(Finding(
                check="render", formula_id=fid, severity="error",
                message=err, context={"file": path.name},
            ))
            continue
        latex = result.get("latex", "")

        symbols = [
            s.get("symbol_overwrite") or s.get("symbol") or s.get("id", "?")
            for s in result.get("variables", [])
        ]
        for sym in symbols:
            if (isinstance(sym, str) and "_" in sym and not sym.startswith("\\")
                    and re.search(r"[a-z]_[a-z]", sym)):
                findings.append(Finding(
                    check="render", formula_id=fid, severity="warning",
                    message=f"snake_case leaked into symbol {sym!r}",
                    context={"file": path.name, "symbol": sym},
                ))
            if sym in _BAD_SYMBOL_LITERALS:
                findings.append(Finding(
                    check="render", formula_id=fid, severity="warning",
                    message=f"bare literal symbol {sym!r}",
                    context={"file": path.name, "symbol": sym},
                ))

        for pat, label in _MALFORMED_PATTERNS:
            if re.search(pat, latex):
                findings.append(Finding(
                    check="render", formula_id=fid, severity="warning",
                    message=f"malformed latex pattern {label!r}",
                    context={"file": path.name, "pattern": label},
                ))
    return findings


def check_semantic(conn, formulas) -> list[Finding]:
    """LHS root misuse and snake_case-only symbol overrides."""
    findings = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        if not eq:
            continue
        tokens = tokenize(conn, eq)
        if not tokens:
            continue
        first = tokens[0]
        if first.get("token_kind") == "quantity":
            qid = first.get("quantity_id")
            if qid == "dimensionless":
                findings.append(Finding(
                    check="semantic", formula_id=fid, severity="warning",
                    message="`dimensionless` used as the LHS quantity",
                    context={"file": path.name},
                ))
            elif qid == "drop":
                findings.append(Finding(
                    check="semantic", formula_id=fid, severity="warning",
                    message="`drop` used as the LHS quantity",
                    context={"file": path.name},
                ))

        for entry in data.get("symbol_overrides") or []:
            if not isinstance(entry, dict):
                continue
            for qid, sym in entry.items():
                if isinstance(sym, str) and re.search(r"[a-z][a-z]+_[a-z][a-z]+", sym):
                    findings.append(Finding(
                        check="semantic", formula_id=fid, severity="warning",
                        message=f"symbol override maps {qid!r} to a snake_case id {sym!r}",
                        context={"file": path.name, "qid": qid, "symbol": sym},
                    ))
    return findings


# Dedupe helpers (ported from dedupe_report.py / dedupe_find.py / dedupe_names.py).
_COMMUTATIVE_OPS = {"add", "mul"}


def _node_value(node):
    if node.kind == "operator":
        return node.operator_id
    if node.kind == "quantity":
        return node.quantity_id
    if node.kind == "constant":
        return node.constant_id
    if node.kind == "number":
        return str(node.value)
    return node.kind


def _normalized(node, sort_commutative=False) -> str:
    root = _node_value(node)
    if node.kind != "operator" or not node.children:
        return root
    parts = [_normalized(c, sort_commutative) for c in node.children]
    if sort_commutative and root in _COMMUTATIVE_OPS:
        parts = sorted(parts)
    return f"({root} {' '.join(parts)})"


def _all_leaves(node):
    if node.kind != "operator" or not node.children:
        return [_node_value(node)]
    out = []
    for child in node.children:
        out.extend(_all_leaves(child))
    return out


def check_duplicates(conn, formulas) -> list[Finding]:
    """Duplicates by exact equation, normalized tree, and yaml `name`."""
    findings = []
    loaded = []  # (fid, path, data, token_count_hint)

    parsed = []
    for path, data in formulas:
        if "_yaml_error" in data:
            continue
        fid = path.stem
        eq = (data.get("equation") or "").strip()
        tokens = tokenize(conn, eq)
        tree = reduce(conn, tokens) if tokens else None
        parsed.append({
            "fid": fid,
            "name": data.get("name", ""),
            "equation": eq,
            "tree": tree,
            "norm": _normalized(tree, sort_commutative=True) if tree else None,
            "vars": sorted(set(_all_leaves(tree))) if tree else [],
        })

    # Exact equation duplicates.
    by_eq = defaultdict(list)
    for p in parsed:
        by_eq[p["equation"]].append(p)
    for eq_str, group in by_eq.items():
        if len(group) > 1:
            findings.append(Finding(
                check="duplicates", severity="warning",
                message=f"exact equation duplicated {len(group)}x",
                context={
                    "kind": "exact",
                    "equation": eq_str,
                    "files": [p["fid"] for p in group],
                },
            ))

    # Structural (normalized-tree) duplicates — only distinct equations.
    by_norm = defaultdict(list)
    for p in parsed:
        if p["norm"] is not None:
            by_norm[p["norm"]].append(p)
    for norm, group in by_norm.items():
        if len(group) > 1 and len({p["equation"] for p in group}) > 1:
            findings.append(Finding(
                check="duplicates", severity="warning",
                message=f"structural duplicate ({len(group)} files)",
                context={
                    "kind": "structural",
                    "normalized": norm,
                    "files": [p["fid"] for p in group],
                },
            ))

    # Name collisions.
    by_name = defaultdict(list)
    for p in parsed:
        if p["name"]:
            by_name[p["name"]].append(p["fid"])
    for name, files in by_name.items():
        if len(files) > 1:
            findings.append(Finding(
                check="duplicates", severity="warning",
                message=f"name {name!r} shared by {len(files)} files",
                context={"kind": "name", "name": name, "files": files},
            ))
    return findings


# ── Registry & driver ──────────────────────────────────────────────────────

ALL_CHECKS = {
    "yaml_schema": check_yaml_schema,
    "overrides": check_overrides,
    "refs": check_refs,
    "dim_lhs": check_dim_lhs,
    "dim_rhs": check_dim_rhs,
    "dim_strict": check_dim_strict,
    "render": check_render,
    "semantic": check_semantic,
    "duplicates": check_duplicates,
}


def _severity_at_or_above(sev, floor) -> bool:
    return _SEVERITY_ORDER.get(sev, 0) >= _SEVERITY_ORDER.get(floor, 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Unified audit of formula YAMLs against the Scifind pipeline.",
    )
    parser.add_argument(
        "--check", action="append", choices=sorted(ALL_CHECKS),
        default=None, metavar="NAME",
        help="run only this check (repeatable); default is all checks",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit findings as JSON instead of human-readable text",
    )
    parser.add_argument(
        "--severity", choices=sorted(_SEVERITY_ORDER), default="info",
        help="only gate (exit-code) findings at or above this severity "
             "(default info)", metavar="LEVEL",
    )
    args = parser.parse_args()

    try:
        conn = open_database()
    except Exception as e:  # noqa: BLE001
        print(f"audit.py: failed to open database: {e}", file=sys.stderr)
        return 2

    selected = sorted(ALL_CHECKS) if args.check is None else sorted(args.check)

    if not FORMULAS_DIR.is_dir():
        print(f"audit.py: missing formulas directory {FORMULAS_DIR}", file=sys.stderr)
        return 2
    if not TOPIC_YAML.is_file():
        print(f"audit.py: missing topic tree {TOPIC_YAML}", file=sys.stderr)
        return 2

    formulas = load_formulas()
    all_findings = []
    for name in selected:
        try:
            all_findings.extend(ALL_CHECKS[name](conn, formulas))
        except Exception as e:  # noqa: BLE001
            conn.close()
            print(
                f"audit.py: check {name!r} crashed: {type(e).__name__}: {e}",
                file=sys.stderr,
            )
            return 2
    conn.close()

    # Stable output order: formula id, then check.
    all_findings.sort(key=lambda f: (f.formula_id, f.check, f.message))

    gated = [
        f for f in all_findings if _severity_at_or_above(f.severity, args.severity)
    ]

    if args.json:
        payload = {
            "formula_dir": str(FORMULAS_DIR),
            "checks_run": selected,
            "severity_floor": args.severity,
            "files_scanned": len(formulas),
            "findings": [asdict(f) for f in all_findings],
            "gated": [asdict(f) for f in gated],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        by_check = defaultdict(list)
        for f in all_findings:
            by_check[f.check].append(f)
        print(f"Scanned {len(formulas)} formula YAMLs; ran checks: "
              f"{', '.join(selected)}")
        for check in selected:
            items = by_check.get(check, [])
            print(f"\n[ {check} ]  ({len(items)} findings)")
            for f in items:
                loc = f.formula_id or "(dataset)"
                print(f"  {f.severity:7s} {loc}")
                if f.message:
                    print(f"            {f.message}")
        print(f"\nTotal findings: {len(all_findings)}; "
              f"gating (>= {args.severity}): {len(gated)}")

    return 1 if gated else 0


if __name__ == "__main__":
    sys.exit(main())
