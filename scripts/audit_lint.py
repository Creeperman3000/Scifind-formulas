"""Build a working list of current state issues by re-running preview_equation for every YAML."""
import sys, os, glob, yaml
sys.path.insert(0, '/home/admin/Projects/Scifind')
from scifind_lib import open_database, preview_equation

conn = open_database()
formulas_dir = '/home/admin/equations/formulas'

issues = {}
for fpath in sorted(glob.glob(os.path.join(formulas_dir, '*.yaml'))):
    with open(fpath) as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        continue
    name = data.get('name', os.path.basename(fpath))
    eq = data.get('equation', '')
    try:
        result = preview_equation(conn, eq)
    except Exception as e:
        issues[name] = [f"EXCEPTION: {e}"]
        continue
    if result.get('error'):
        issues[name] = [f"ERROR: {result['error']}"]
        continue
    latex = result.get('latex', '')
    syms = result.get('variables', [])
    sym_list = []
    for s in syms:
        ov = s.get('symbol_overwrite') or s.get('symbol') or s.get('id', '?')
        sym_list.append(ov)
    # Inspect the symbol map for problems
    # 1) Snake_case leakage
    import re
    bad_snake = [s for s in sym_list if '_' in s and not s.startswith('\\') and re.search(r'[a-z]_[a-z]', s)]
    if bad_snake:
        issues.setdefault(name, []).append(f"SNAKE_CASE: {bad_snake}")
    # 2) literal 'dimensionless' or '2' or 'pi' or '0' etc appearing in rendered output
    bad_dim = [s for s in sym_list if s in ('dimensionless', '0', '1', '2', 'pi', 'e', 'dimension')]
    if bad_dim:
        issues.setdefault(name, []).append(f"BAD_LITERAL: {bad_dim}")
    # 3) `number_of_particles` from the snake_case lib scan
    if 'number_of_particles' in latex:
        issues.setdefault(name, []).append("SNAKE_IN_LATEX: number_of_particles")

print(f"Total formulas with current issues: {len(issues)}")
for n, i in sorted(issues.items()):
    print(f"  {n}: {'; '.join(i)}")
