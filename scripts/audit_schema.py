#!/usr/bin/env python3
"""Final comprehensive audit of all formula YAML files against Scifind schema."""

import os
import sys
import yaml
from collections import defaultdict

sys.path.insert(0, '/home/admin/Projects/Scifind')
from scifind_lib import open_database, preview_equation, parse_equation

FORMULAS_DIR = '/home/admin/equations/formulas'
TOPIC_YAML   = '/home/admin/equations/topic.yaml'
QUANTITY_TXT = '/home/admin/equations/quantity.txt'
CONSTANT_TXT = '/home/admin/equations/constant.txt'
OPERATOR_CSV = '/home/admin/equations/operator.csv'

# ── Load valid sets ────────────────────────────────────────────────────────

def load_leaf_topics(path):
    with open(path) as f:
        data = yaml.safe_load(f)
    leaves = set()
    def walk(node):
        if not isinstance(node, dict): return
        children = node.get('children', [])
        if not children:
            tid = node.get('id')
            if tid: leaves.add(tid)
            return
        for child in children:
            if isinstance(child, dict): walk(child)
    walk(data)
    return leaves

def load_id_set(path):
    with open(path) as f:
        return {line.strip() for line in f if line.strip() and not line.startswith('#')}

def load_operators(path):
    ops = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            parts = line.split(',')
            if parts:
                ops.add(parts[0].strip('"\''))
    return ops

valid_topics     = load_leaf_topics(TOPIC_YAML)
valid_quantities = load_id_set(QUANTITY_TXT)
valid_constants  = load_id_set(CONSTANT_TXT)
valid_operators  = load_operators(OPERATOR_CSV)

conn = open_database()

yaml_files = sorted(f for f in os.listdir(FORMULAS_DIR) if f.endswith('.yaml'))
print(f"Total YAML files scanned: {len(yaml_files)}")

# ── Issue accumulators ─────────────────────────────────────────────────────
issues = defaultdict(list)

def record(category, fname, detail=""):
    issues[category].append((fname, detail))

# ── Per-file audit ─────────────────────────────────────────────────────────
for fname in yaml_files:
    fpath = os.path.join(FORMULAS_DIR, fname)
    stem  = fname[:-5]

    # 1. YAML parse
    try:
        with open(fpath) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        record('yaml_syntax_error', fname, str(e))
        continue
    if not isinstance(data, dict):
        record('yaml_syntax_error', fname, f'parsed to {type(data).__name__}')
        continue

    # 2. Missing id
    fid = data.get('id')
    if not fid:
        record('missing_id', fname)

    # 3. Filename / id mismatch
    if fid and fid != stem:
        record('filename_id_mismatch', fname, f'stem={stem!r} id={fid!r}')

    # 4. Topic
    topic = data.get('topic')
    if not topic:
        record('missing_topic', fname)
    elif topic not in valid_topics:
        record('invalid_topic', fname, f'topic={topic!r}')

    # 5. Equation
    eq = data.get('equation')
    if not eq:
        record('missing_equation', fname)
        continue

    # 6. RPN evaluation (via preview_equation which calls evaluate_rpn)
    try:
        result = preview_equation(conn, eq)
        err = result.get('error', '')
        if err:
            if 'RPN did not reduce' in err or 'underflow' in err:
                record('rpn_reduction_error', fname, err)
            else:
                record('equation_parse_error', fname, err)
    except Exception as e:
        record('equation_parse_error', fname, str(e))

    # 7. Difficulty
    diff = data.get('difficulty')
    if diff is None:
        record('missing_difficulty', fname)
    else:
        try:
            dval = int(diff)
            if dval < 1 or dval > 10:
                record('difficulty_out_of_range', fname, f'difficulty={diff}')
        except (ValueError, TypeError):
            record('difficulty_out_of_range', fname, f'difficulty={diff!r}')

    # 8-9. Symbol/name overrides vs quantity token count
    so_has_field = 'symbol_overrides' in data
    no_has_field = 'name_overrides' in data
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
        # symbol_overrides
        if so_has_field and len(so) != num_qty:
            record('mismatched_symbol_overrides', fname,
                   f'so count={len(so)} vs qty tokens={num_qty}')
        # name_overrides
        if no_has_field and len(no) != num_qty:
            record('mismatched_name_overrides', fname,
                   f'no count={len(no)} vs qty tokens={num_qty}')

        # Check if overrides reference quantities not in the equation
        so_qids = set()
        for entry in so:
            if isinstance(entry, dict):
                for k, v in entry.items():
                    if k:  # the key is the quantity_id
                        so_qids.add(k)
        no_qids = set()
        for entry in no:
            if isinstance(entry, dict):
                for k, v in entry.items():
                    if k:
                        no_qids.add(k)

        actual_qids = set(qt.get('quantity_id') for qt in qty_tokens)
        phantom_so = so_qids - actual_qids
        phantom_no = no_qids - actual_qids
        if phantom_so:
            record('symbol_override_phantom_quantity', fname,
                   f'so references qids not in equation: {sorted(phantom_so)}')
        if phantom_no:
            record('name_override_phantom_quantity', fname,
                   f'no references qids not in equation: {sorted(phantom_no)}')

    # 10-12. Referenced quantities / constants / operators
    try:
        tokens = parse_equation(conn, eq)
    except Exception:
        tokens = []
    for tok in tokens:
        kind = tok.get('token_kind')
        if kind == 'quantity':
            qid = tok.get('quantity_id')
            if qid and qid not in valid_quantities:
                record('unknown_quantity', fname, f'qid={qid!r} not in quantity.txt')
        elif kind == 'constant':
            cid = tok.get('constant_id')
            if cid and cid not in valid_constants:
                record('unknown_constant', fname, f'cid={cid!r} not in constant.txt')
        elif kind == 'operator':
            oid = tok.get('operator_id')
            if oid and oid not in valid_operators:
                record('unknown_operator', fname, f'oid={oid!r} not in operator.csv')

# ── Print report ───────────────────────────────────────────────────────────
print(f"\n{'='*72}")
print("COMPREHENSIVE AUDIT REPORT - ALL ISSUES FOUND")
print(f"{'='*72}")

if not issues:
    print("\n  NO ISSUES FOUND - all 1223 files pass all checks.")
else:
    for cat in sorted(issues.keys()):
        entries = issues[cat]
        print(f"\n  [{cat}]  ({len(entries)} files)")
        print(f"  {'-'*68}")
        for fname, detail in sorted(entries):
            print(f"    {fname}")
            if detail:
                print(f"      >>> {detail}")

print(f"\n{'='*72}")
print("SUMMARY")
print(f"{'='*72}")
print(f"  Files scanned:      {len(yaml_files)}")
total_issues = sum(len(v) for v in issues.values())
print(f"  Total issues found: {total_issues}")
print(f"  Issue categories:   {len(issues)}")
if issues:
    print()
    for cat in sorted(issues.keys()):
        print(f"    {cat}: {len(issues[cat])}")
