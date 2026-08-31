# Creating formulas

- create a formula in `../formulas/<id>.yaml`
- migrate YAMLs: `python3 build_seed.py`
- check a single formula's LaTeX `python3 latex.py <formula_id>`
- see all formulas `../formulas.yaml`:
- `python3 latex_all.py`
- verification `python3 audit.py`

## Scripts

### `build_seed.py`
Migrates every YAML to Scifind's `seed.sql`.
Fails on the first broken YAML.
A backup is written to `seed.sql.bak` on success.

### `latex.py`
Used to render the LaTeX of one formula by its id: `python3 render_latex.py <formula_id>`.

### `latex_all.py`
Renders every formula and writes `../formulas.yaml

### `audit.py`
A unified audit tool. Runs a set of independent checks over every formula
YAML and reports findings (optionally as JSON); see `--help` for details.

```
python3 audit.py                       # run every check
python3 audit.py --check yaml_schema   # run one check
python3 audit.py --json                # machine-readable findings
python3 audit.py --severity error      # only gate on error-severity findings
```

Checks:

| check        | purpose                                                        |
|--------------|----------------------------------------------------------------|
| `yaml_schema`| YAML parses; `id` present and matches filename; topic is a leaf of `../tree.yaml`; equation present; difficulty in [1,10]. |
| `overrides`  | symbol/name override list lengths match quantity-token counts; no override references a quantity absent from the equation. |
| `refs`       | every quantity / constant / operator in the equation exists in `scifind.db`. |
| `dim_lhs`    | LHS dimensions match at least one quantity in the database.     |
| `dim_rhs`    | RHS sides of a relational root have mutually consistent dimensions. |
| `dim_strict` | full dimension walk that surfaces the mismatches the renderer swallows. |
| `render`     | parses + previews cleanly; no snake_case leakage in symbols; no malformed latex (`pi2`, `theta2`, `dimensionless^2`, ...). |
| `semantic`   | LHS root is not `dimensionless` / `drop`; no snake_case-only symbol overrides. |
| `duplicates` | formulas duplicated by exact equation, normalized tree, or yaml `name`. |

Exit codes:
- `0` all requested checks pass
- `1` at least one finding
- `2` the tool itself crashed

### `yaml_overrides.py`
Shared helper, should not be run alone.
Translates a YAML's position-indexed `symbol_overrides` and `name_overrides`
into `{qid|label|pos:{symbol, name_overwrite}}` dict that Scifind's `build_create_sql` and
`parse_and_preview_equation` support.
