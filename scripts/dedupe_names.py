#!/usr/bin/env python3
"""Find formulas whose `name:` field collides with another file's `name:`.

These are a hidden source of bugs because render_latex.py keys its output
dict by `name`, so a collision silently overwrites the earlier file's
output.

Run from the formulas/ directory:
    python3 /home/admin/equations/scripts/find_name_collisions.py
"""
import glob
import os
import sys
from collections import defaultdict

import yaml

FORMULAS_DIR = "/home/admin/equations/formulas"


def main():
    by_name = defaultdict(list)
    for fpath in sorted(glob.glob(os.path.join(FORMULAS_DIR, "*.yaml"))):
        with open(fpath) as f:
            data = yaml.safe_load(f) or {}
        name = data.get("name")
        fid = data.get("id")
        if name:
            by_name[name].append((fid, os.path.basename(fpath)))
    collisions = {k: v for k, v in by_name.items() if len(v) > 1}
    if not collisions:
        print("No name collisions found.")
        return 0
    print(f"Found {len(collisions)} name collisions:")
    for name, files in sorted(collisions.items()):
        print(f"\n  Name: {name!r}")
        for fid, fname in files:
            print(f"    - id={fid:40s} file={fname}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
