"""Find all formulas with malformed patterns like pi2, theta2, m2, etc."""
import os, sys, glob, yaml, re
sys.path.insert(0, '/home/admin/Projects/Scifind')
from scifind_lib import open_database, preview_equation

conn = open_database()
formulas_dir = '/home/admin/equations/formulas'

# Patterns to flag
MALFORMED_PATTERNS = [
    (r'\\pi\s*2', 'pi2'),
    (r'\\theta\s*2', 'theta2'),
    (r'\\alpha\s*2', 'alpha2'),
    (r'\\alpha\s*3', 'alpha3'),
    (r'\\gamma\s*2', 'gamma2'),
    (r'\\gamma\s*4', 'gamma4'),
    (r'\\varepsilon\s*0', 'varepsilon0'),
    (r'\\eta\s*2', 'eta2'),
    (r'\\eta\s*3', 'eta3'),
    (r'\\omega\s*2', 'omega2'),
    (r'\\omega\s*0', 'omega0'),
    (r'\\sigma\s*2', 'sigma2'),
    (r'\\sigma\s*4', 'sigma4'),
    (r'\\sigma\s*8', 'sigma8'),
    (r'\\tau\s*2', 'tau2'),
    (r'\\lambda\s*2', 'lambda2'),
    (r'\\nu\s*2', 'nu2'),
    (r'\\Delta\s*2', 'Delta2'),
    (r'\\Delta\s*3', 'Delta3'),
    (r'\\Delta\s*4', 'Delta4'),
    (r'\\sum\s*2', 'sum2'),
    (r'\\prod\s*2', 'prod2'),
    (r'\\int\s*2', 'int2'),
    (r'\\frac\s*2', 'frac2'),
    (r'\\oint\s*2', 'oint2'),
    (r'\\sqrt\s*2', 'sqrt2'),
    (r'\\partial\s*2', 'partial2'),
    (r'\\dot\s*2', 'dot2'),
    (r'\\hat\s*2', 'hat2'),
    (r'\\vec\s*2', 'vec2'),
    (r'dimensionless\^?2', 'dimensionless2'),
    (r'dimensionless\^?3', 'dimensionless3'),
    (r'dimensionless\^?4', 'dimensionless4'),
    (r'dimensionless\^?½', 'dimensionless_1_2'),
    (r'dimensionless\^?⅓', 'dimensionless_1_3'),
]

results = []
for fpath in sorted(glob.glob(os.path.join(formulas_dir, '*.yaml'))):
    with open(fpath) as f:
        data = yaml.safe_load(f)
    if not data or not isinstance(data, dict):
        continue
    name = data.get('name', '')
    eq = data.get('equation', '')
    try:
        result = preview_equation(conn, eq)
        latex = result.get('latex', '')
    except Exception as e:
        latex = f"ERROR: {e}"
    found = []
    for pat, label in MALFORMED_PATTERNS:
        if re.search(pat, latex):
            found.append(label)
    if found:
        results.append((name, fpath, found, latex[:200]))

print(f"Found {len(results)} formulas with malformed patterns:")
for n, f, found, l in results:
    print(f"  {n}: {found}")
    print(f"    latex: {l}")
