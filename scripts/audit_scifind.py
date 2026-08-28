#!/usr/bin/env python3
"""Comprehensive audit of all formula YAML files against the Scifind schema.

Source of truth for quantities / constants / operators is the Scifind
database (`scifind.db`), not the legacy on-disk CSV/TXT files. The legacy
files are kept on disk for human review but no longer consulted by this
script.

If the DB schema changes, re-run this audit to catch formula YAMLs that
reference operators, quantities, or constants that no longer exist.
"""
from __future__ import annotations

import os
import sys
import yaml
from collections import defaultdict

sys.path.insert(0, '/home/admin/Projects/Scifind')
from scifind_lib import open_database, preview_equation
from scifind_lib.parser import parse_equation

# ── Paths ──────────────────────────────────────────────────────────────────
FORMULAS_DIR = '/home/admin/equations/formulas'
TOPIC_YAML   = '/home/admin/equations/tree.yaml'


# ── Load valid sets ────────────────────────────────────────────────────────

def load_leaf_topics(path):
    """Extract ALL leaf topic IDs from tree.yaml.

    tree.yaml is a hierarchical topic tree; only leaves are valid `topic:`
    values in a formula YAML. The file uses a slightly unusual indented list
    format that PyYAML flattens to a single string per branch, so we parse
    the indentation manually: a line is a leaf iff it has no more-indented
    children beneath it.
    """
    raw_lines = [l for l in open(path).read().split('\n') if l.lstrip().startswith('-')]
    items = []
    for line in raw_lines:
        indent = len(line) - len(line.lstrip())
        name = line.lstrip()[1:].strip()
        items.append((indent, name))
    # An item is a leaf if no later item has greater indent
    leaves = set()
    for i, (indent, name) in enumerate(items):
        is_leaf = all(items[j][0] <= indent for j in range(i + 1, len(items)))
        if is_leaf:
            leaves.add(name)
    return leaves


def load_db_id_sets(conn):
    """Pull the current quantity / constant / operator ID sets from the DB.

    This is the single source of truth — `operator.csv`, `quantity.txt`,
    `constant.txt` are no longer consulted. If the DB schema adds an
    operator or quantity, this audit picks it up automatically; if it
    removes one, every formula referencing it fails the audit.
    """
    quantities = {r[0] for r in conn.execute("SELECT id FROM quantity")}
    constants  = {r[0] for r in conn.execute("SELECT id FROM constant")}
    operators  = {r[0] for r in conn.execute("SELECT id FROM operator")}
    return quantities, constants, operators


# ── Load everything ────────────────────────────────────────────────────────
valid_topics = load_leaf_topics(TOPIC_YAML)
conn = open_database()
valid_quantities, valid_constants, valid_operators = load_db_id_sets(conn)

# ── Count files ────────────────────────────────────────────────────────────
yaml_files = sorted(f for f in os.listdir(FORMULAS_DIR) if f.endswith('.yaml'))
print(f"Total YAML files: {len(yaml_files)}")
print(f"Source of truth: scifind.db  "
      f"(quantities={len(valid_quantities)}, constants={len(valid_constants)}, operators={len(valid_operators)})")

# ── Issue trackers ─────────────────────────────────────────────────────────
issues = defaultdict(list)  # issue_key -> [(filename, detail)]


def record(issue_key, filename, detail=""):
    issues[issue_key].append((filename, str(detail)))


rpn_failures = 0
rpn_successes = 0
rpn_errors_per_file = {}

# ── Process each file ──────────────────────────────────────────────────────
for fname in yaml_files:
    fpath = os.path.join(FORMULAS_DIR, fname)
    stem = fname[:-5]  # remove .yaml

    # 1. YAML parse
    try:
        with open(fpath) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        record('yaml_syntax_error', fname, str(e))
        continue

    if not isinstance(data, dict):
        record('yaml_syntax_error', fname, 'YAML parsed to non-dict: %s' % type(data).__name__)
        continue

    # 2. Missing id
    fid = data.get('id')
    if not fid:
        record('missing_id', fname, 'id field is missing or empty')

    # 3. Filename doesn't match id
    if fid and fid != stem:
        record('filename_id_mismatch', fname, f'filename stem="{stem}" but id="{fid}"')

    # 4. Missing/invalid topic
    topic = data.get('topic')
    if not topic:
        record('missing_topic', fname, 'topic field is missing or empty')
    elif topic not in valid_topics:
        record('invalid_topic', fname, f'topic="{topic}" is not a leaf topic in topic.yaml')

    # 5. Missing equation
    eq = data.get('equation')
    if not eq:
        record('missing_equation', fname, 'equation field is missing or empty')
        continue

    # 6. RPN parse & evaluation via preview_equation
    try:
        result = preview_equation(conn, eq)
        err = result.get('error', '')
        if err:
            rpn_failures += 1
            rpn_errors_per_file[fname] = err
            if 'RPN did not reduce' in err or 'underflow' in err:
                record('rpn_reduction_error', fname, err)
            else:
                record('equation_parse_error', fname, err)
        else:
            rpn_successes += 1
    except Exception as e:
        rpn_failures += 1
        rpn_errors_per_file[fname] = str(e)
        record('equation_parse_error', fname, str(e))

    # 7. Difficulty
    diff = data.get('difficulty')
    if diff is None:
        record('missing_difficulty', fname, 'difficulty field is missing')
    else:
        try:
            dval = int(diff)
            if dval < 1 or dval > 10:
                record('difficulty_out_of_range', fname, f'difficulty={diff} (should be 1-10)')
        except (ValueError, TypeError):
            record('difficulty_out_of_range', fname, f'difficulty={diff!r} is not an integer')

    # 8. Symbol overrides vs quantity token count
    so = data.get('symbol_overrides', []) or []
    no = data.get('name_overrides', []) or []

    try:
        tokens = parse_equation(conn, eq)
        qty_tokens = [t for t in tokens if t.get('token_kind') == 'quantity']
        num_qty = len(qty_tokens)
    except Exception:
        qty_tokens = []
        num_qty = -1

    if num_qty >= 0:
        if len(so) != num_qty:
            record('mismatched_symbol_overrides', fname,
                   f'symbol_overrides count={len(so)} but quantity tokens={num_qty}')
        if len(no) != num_qty:
            record('mismatched_name_overrides', fname,
                   f'name_overrides count={len(no)} but quantity tokens={num_qty}')

    # 9-11. Referenced quantities, constants, operators — looked up against
    # the DB, not on-disk CSV/TXT.
    try:
        tokens = parse_equation(conn, eq)
    except Exception:
        tokens = []

    for tok in tokens:
        kind = tok.get('token_kind')
        if kind == 'quantity':
            qid = tok.get('quantity_id')
            if qid and qid not in valid_quantities:
                record('unknown_quantity', fname, f'quantity="{qid}" not in scifind.db quantity table')
        elif kind == 'constant':
            cid = tok.get('constant_id')
            if cid and cid not in valid_constants:
                record('unknown_constant', fname, f'constant="{cid}" not in scifind.db constant table')
        elif kind == 'operator':
            oid = tok.get('operator_id')
            if oid and oid not in valid_operators:
                record('unknown_operator', fname, f'operator="{oid}" not in scifind.db operator table')


# ── Report ─────────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print("AUDIT REPORT")
print(f"{'='*70}")
print(f"Total files checked: {len(yaml_files)}")
print(f"RPN successes:       {rpn_successes}")
print(f"RPN failures:        {rpn_failures}")

for key in sorted(issues.keys()):
    entries = issues[key]
    print(f"\n{'─'*70}")
    print(f"ISSUE: {key}  ({len(entries)} files)")
    print(f"{'─'*70}")
    for fname, detail in sorted(entries):
        print(f"  {fname}")
        if detail:
            print(f"    → {detail}")

print(f"\n{'='*70}")
print("SUMMARY")
print(f"{'='*70}")
print(f"Total files:              {len(yaml_files)}")
print(f"Total issues found:       {sum(len(v) for v in issues.values())}")
print(f"Issue categories:         {len(issues)}")
for key in sorted(issues.keys()):
    print(f"  {key}: {len(issues[key])}")
