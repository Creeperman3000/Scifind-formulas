"""Find duplicate formulas by their rendered LaTeX."""
import os, glob, yaml, sys
sys.path.insert(0, '/home/admin/Projects/Scifind')
from scifind_lib import open_database, preview_equation

conn = open_database()
formulas_dir = '/home/admin/equations/formulas'

latex_to_files = {}
for fpath in sorted(glob.glob(os.path.join(formulas_dir, '*.yaml'))):
    with open(fpath) as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        continue
    eq = data.get('equation', '')
    try:
        result = preview_equation(conn, eq)
        latex = result.get('latex', '')
        if result.get('error'):
            continue
    except Exception:
        continue
    if not latex or latex.startswith('ERROR'):
        continue
    latex_to_files.setdefault(latex, []).append((fpath, data.get('name', '')))

duplicates = [(l, fs) for l, fs in latex_to_files.items() if len(fs) > 1]
print(f"Found {len(duplicates)} duplicate groups:")
for l, fs in duplicates:
    print(f"  LaTeX: {l[:100]}")
    for f, n in fs:
        print(f"    {f}: {n}")
