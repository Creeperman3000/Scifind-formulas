"""Smoke test: render all formulas and dump LaTeX output to a file.

Useful for spotting regressions after changes to the schema or RPN engine.
Run from the project root after `python3 scifind_cli.py init`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scifind_lib import open_database, render_formula


def main():
    conn = open_database()
    rows = conn.execute("SELECT id, name FROM formula ORDER BY id").fetchall()
    out_lines = []
    for row in rows:
        fid = row["id"]
        try:
            latex = render_formula(conn, fid)
        except Exception as e:
            latex = f"ERROR: {e}"
        out_lines.append(f"{fid:42s} {latex}")
    text = "\n".join(out_lines) + "\n"
    if len(sys.argv) > 1:
        Path(sys.argv[1]).write_text(text, encoding="utf-8")
        print(f"Wrote {len(out_lines)} formulas to {sys.argv[1]}")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
