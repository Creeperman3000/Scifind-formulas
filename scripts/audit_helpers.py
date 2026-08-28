"""Apply fixes from formulas_audit.md to /home/admin/equations/formulas/*.yaml.

Each fix sets equation, symbol_overrides, name_overrides aligned to the
new equation's quantity token positions. Validates by re-rendering via
the existing render_latex pipeline (parse_equation + _evaluate_rpn + _latex_node).
"""
import os, sys, glob, copy, re
sys.path.insert(0, '/home/admin/Projects/Scifind')
sys.path.insert(0, '/home/admin/equations/scripts')
import yaml
from scifind_lib import open_database, parse_equation, _evaluate_rpn
from render_latex import _latex_node

FORMULAS_DIR = '/home/admin/equations/formulas'

def read_yaml(path):
    with open(path) as f:
        return yaml.safe_load(f)

def write_yaml(path, data):
    with open(path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True,
                  sort_keys=False, width=float('inf'))

def quantity_tokens(eq):
    """Extract quantity token IDs in order (constants don't need overrides)."""
    return re.findall(r'\((\w+)\)', eq)

def render_eq(conn, eq, so, no):
    """Render equation to latex using overrides (list of {qid: val} dicts)."""
    tokens = parse_equation(conn, eq)
    qty_pos = 0
    for tok in tokens:
        if tok['token_kind'] != 'quantity':
            continue
        qty_pos += 1
        idx = qty_pos - 1
        if so and idx < len(so):
            entry = so[idx]
            if isinstance(entry, dict):
                for qid, val in entry.items():
                    if val:
                        tok['symbol_overwrite'] = str(val)
        if no and idx < len(no):
            entry = no[idx]
            if isinstance(entry, dict):
                for qid, val in entry.items():
                    if val:
                        tok['name_overwrite'] = str(val)
    tree = _evaluate_rpn(conn, tokens)
    return _latex_node(tree, conn, 'en-us')

def fix(fname, new_eq, new_so, new_no, desc, conn, verbose=True):
    """Apply a fix: validate render, write YAML if successful."""
    path = os.path.join(FORMULAS_DIR, fname)
    if not os.path.exists(path):
        if verbose: print(f"  SKIP {fname}: not found")
        return False
    try:
        latex = render_eq(conn, new_eq, new_so, new_no)
    except Exception as e:
        if verbose: print(f"  FAIL {fname}: {e}")
        return False
    data = read_yaml(path)
    data['equation'] = new_eq
    data['symbol_overrides'] = new_so
    data['name_overrides'] = new_no
    write_yaml(path, data)
    if verbose: print(f"  FIXED {fname}: {desc}")
    return True

def fix_overrides_only(fname, new_so, new_no, desc, conn, verbose=True):
    """Apply override-only fix; equation unchanged."""
    path = os.path.join(FORMULAS_DIR, fname)
    if not os.path.exists(path):
        if verbose: print(f"  SKIP {fname}: not found")
        return False
    data = read_yaml(path)
    eq = data.get('equation', '')
    so = data.get('symbol_overrides') or []
    no = data.get('name_overrides') or []
    try:
        new_latex = render_eq(conn, eq, new_so, new_no)
    except Exception as e:
        if verbose: print(f"  FAIL {fname}: {e}")
        return False
    data['symbol_overrides'] = new_so
    data['name_overrides'] = new_no
    write_yaml(path, data)
    if verbose: print(f"  FIXED {fname}: {desc}")
    return True

if __name__ == '__main__':
    conn = open_database()
    print("fix_helpers module loaded")