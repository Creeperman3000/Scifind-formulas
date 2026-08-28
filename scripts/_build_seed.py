#!/usr/bin/env python3
"""Migrate /home/admin/equations/formulas/*.yaml to seed.sql.

Mirrors the algorithm used by Scifind's /create page
(`build_create_sql` in scifind_lib.py). The migration replaces the existing
formula / formula_token blocks in seed.sql with the result of running every
yaml file through that pipeline.

Run from any CWD; the script reads from /home/admin/equations/formulas/
and rewrites /home/admin/Projects/Scifind/seed.sql in place (after stashing
the previous copy at seed.sql.bak next to it — but only once every yaml
has processed cleanly).

Usage:
    python3 scripts/migrate_formulas_to_seed.py

Exit codes:
    0  on success
    1  on any problem: a broken/unparseable yaml, missing fields, an id
       that doesn't match its filename, an equation referencing an
       unknown identifier, or a filesystem failure that prevents the
       migration from completing. The error message names the offending
       file; seed.sql is left untouched. Broken files are never
       silently skipped.
"""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import sys
import traceback
from pathlib import Path
from typing import Any

import yaml

# Import Scifind's own parser/SQL-builder so the produced SQL is byte-for-byte
# the same shape as if the user had typed the equation into /create.
SCIFIND_DIR = Path("/home/admin/Projects/Scifind")
sys.path.insert(0, str(SCIFIND_DIR))
import scifind_lib  # noqa: E402
from scifind_lib.parser import parse_equation  # noqa: E402

FORMULAS_DIR = Path("/home/admin/equations/formulas")
SEED_PATH = SCIFIND_DIR / "seed.sql"
SEED_BAK_PATH = SCIFIND_DIR / "seed.sql.bak"
DB_PATH = SCIFIND_DIR / "scifind.db"


def build_overrides(conn, equation: str, sym_ov: Any, nm_ov: Any) -> dict:
    """Translate yaml's position-indexed override lists into the
    ``{<qid>||<pos>: {symbol, name}}`` dict used by build_create_sql.

    yaml distributes each list's entries from left to right across
    occurrences of the same quantity_id in the equation.  Empty-string
    values advance the slot without recording a value (padding).  The
    two lists maintain independent cursors per quantity_id, matching
    the documented yaml semantics.
    """
    overrides: dict[str, dict[str, str]] = {}
    if not (sym_ov or nm_ov):
        return overrides
    tokens = parse_equation(conn, equation)

    occ_pos: dict[str, list[int]] = {}
    for rpn_pos, tok in enumerate(tokens, start=1):
        if tok.get("token_kind") == "quantity":
            qid = tok["quantity_id"]
            occ_pos.setdefault(qid, []).append(rpn_pos)

    def advance(qid: str, val: Any, state: dict[str, int]) -> None:
        if val is None:
            val = ""
        val = str(val)
        positions = occ_pos.get(qid, [])
        cursor = state.get(qid, 0)
        if cursor >= len(positions):
            return
        actual_pos = positions[cursor]
        state[qid] = cursor + 1
        if val == "":
            return
        key = f"{qid}||{actual_pos}"
        overrides.setdefault(key, {})[field] = val

    # Each list owns its own per-qid cursor so an empty-string padding
    # entry in symbol_overrides does not also skip a slot in
    # name_overrides (and vice versa).
    sym_state: dict[str, int] = {}
    nm_state: dict[str, int] = {}
    for ov_list, field, state in (
        (sym_ov, "symbol", sym_state),
        (nm_ov, "name", nm_state),
    ):
        if not isinstance(ov_list, list):
            continue
        for ov in ov_list:
            if not isinstance(ov, dict):
                continue
            for qid, val in ov.items():
                advance(qid, val, state)
    return overrides


class MigrationError(Exception):
    """A yaml file is broken; the migration must abort, not skip."""


def process_yaml(conn, yaml_path: Path) -> tuple[str, str, str, str]:
    """Return (yaml_stem, yaml_id, formula_sql, token_sql).

    Raises :class:`MigrationError` naming the offending file for any
    problem (unparseable yaml, missing fields, id/filename mismatch,
    unknown identifier in the equation, ...).  We deliberately do NOT
    skip broken files: a formula that silently vanishes from seed.sql
    is far harder to notice than a crashed migration.

    ``yaml_id`` is the value of the yaml's ``id:`` field — the same
    identifier the file is named after.  Callers use it as the
    canonical sort key for the produced SQL rows AND as the final
    formula_id that lands in ``seed.sql``.  We deliberately do not let
    ``build_create_sql`` derive the formula_id from the English name:
    that drift would mean the SQL id disagrees with the yaml id and
    the file name.  The three should always be the same string.
    """
    stem = yaml_path.stem
    try:
        data = yaml.safe_load(yaml_path.read_text())
    except Exception as e:
        raise MigrationError(f"{yaml_path.name}: yaml load failed: {e}") from e
    if not isinstance(data, dict):
        raise MigrationError(
            f"{yaml_path.name}: top-level YAML is not a mapping")
    name = (data.get("name") or "").strip()
    topic = (data.get("topic") or "").strip()
    equation = (data.get("equation") or "").strip()
    description = (data.get("description") or "").strip()
    yaml_id = (data.get("id") or "").strip()
    difficulty = data.get("difficulty", 2)
    if not name or not topic or not equation:
        raise MigrationError(
            f"{yaml_path.name}: missing name/topic/equation")
    if not yaml_id:
        raise MigrationError(f"{yaml_path.name}: missing `id:` field")
    if yaml_id != stem:
        # The yaml's `id:` field must match the filename (without
        # extension).  Anything else is a footgun: the user has
        # already established filename == yaml id == seed.sql id as
        # the invariant this script enforces.
        raise MigrationError(
            f"{yaml_path.name}: yaml id {yaml_id!r} does not match "
            f"filename {stem!r}")

    sym_ov = data.get("symbol_overrides") or []
    nm_ov = data.get("name_overrides") or []
    try:
        overrides = build_overrides(conn, equation, sym_ov, nm_ov)
    except Exception as e:
        raise MigrationError(
            f"{yaml_path.name}: equation could not be parsed "
            f"({e!r}); fix the equation or extend the vocabulary"
        ) from e

    # `links` is a list of URL strings stored as a plain JSON array in the
    # formula.links column.  Tolerate non-list values and blank entries so a
    # malformed yaml degrades to NULL rather than failing the migration.
    raw_links = data.get("links")
    if isinstance(raw_links, list):
        links = [u.strip() for u in raw_links if isinstance(u, str) and u.strip()]
    else:
        links = []
    links = links or None

    try:
        f_sql, t_sql = scifind_lib.build_create_sql(
            conn,
            name_en=name,
            topic=topic,
            difficulty=difficulty,
            equation=equation,
            overrides=overrides,
            description=description or None,
            links=links,
        )
    except Exception as e:
        raise MigrationError(
            f"{yaml_path.name}: build_create_sql failed: {e!r}") from e

    # build_create_sql derived the formula_id from the English name.
    # Rewrite it to match the yaml's `id:` field so filename, yaml
    # id, and SQL formula_id all agree.  The /create page algorithm
    # always emits exactly one formula row, so the formula SQL has
    # exactly one `('<id>',` occurrence; the token SQL may have many.
    derived_fid_match = re.search(r"\('([^']+)'", f_sql)
    if not derived_fid_match:
        raise MigrationError(
            f"{yaml_path.name}: could not parse formula_id from SQL")
    derived_fid = derived_fid_match.group(1)
    if derived_fid != yaml_id:
        f_sql = f_sql.replace(f"('{derived_fid}',", f"('{yaml_id}',", 1)
        t_sql = t_sql.replace(f"('{derived_fid}',", f"('{yaml_id}',")
    return stem, yaml_id, f_sql, t_sql


def split_seed(seed_text: str) -> str:
    """    Drop every existing INSERT block targeting
    formula / formula_token / formula_relations, plus the comment header
    banner that marks a previous migration's output.

    Returns ``seed_text`` with those blocks removed. The migration
    replaces the formulas wholesale, so the old formula rows, their
    tokens, and the relations (which all key off the old formula
    ids) must all go.  Stripping the migration banner lets the
    script be idempotent across multiple runs.

    Banner stripping is conservative: when we see a banner comment,
    we drop every banner+INSERT bundle following it until the next
    line that doesn't match either pattern.  In practice the
    migration always emits exactly one banner + one formula block +
    one formula_token block, with no intervening blank lines, so a
    simple two-pass approach is sufficient.
    """
    lines = seed_text.splitlines(keepends=True)

    # Pass 1: identify the line ranges that belong to a previous
    # migration output and to the original seed's formula/relation
    # blocks.  We build a set of indices to drop.
    drop = set()
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.lstrip().lower()
        if (
            stripped.startswith("insert or ignore into formula ")
            or stripped.startswith("insert or ignore into formula_token ")
            or stripped.startswith("insert or ignore into formula_relation ")
            or stripped.startswith("insert or ignore into formula_relations ")
        ):
            j = i
            while j < n and lines[j].strip() != ";":
                drop.add(j)
                j += 1
            if j < n:
                drop.add(j)  # the terminator `;`
            i = j + 1
            continue
        # Lines that look like part of the migration banner.  The
        # banner spans 3 lines: "-- ====...", "-- Formulas migrated
        # from ...", "-- ====...".  We strip every line that's a
        # banner separator ("-- =...") or that mentions the migration
        # source — that way any partially-stripped banner left over
        # from a previous broken run is also removed.
        if line.startswith("--") and (
            "Formulas migrated from" in line
            or "via build_create_sql" in line
            or line.rstrip().endswith("=" * 10)
            or line.rstrip().endswith("=" * 20)
        ):
            j = i
            # Drop everything from the banner up to (and including) the
            # terminator of the matching INSERT block.  The migration
            # always emits banner -> blank -> banner -> ... -> blank ->
            # INSERT (formula) -> INSERT (formula_token); a previous
            # broken run may stack banners without blanks, so we treat
            # any further banner, blank, or INSERT-marker line as part
            # of the same dropped region until we hit a `;` that
            # closes a formula/formula_token INSERT.
            in_insert_block = False
            while j < n:
                cur = lines[j]
                s = cur.lstrip().lower()
                if s.startswith("insert or ignore into formula ") or s.startswith(
                    "insert or ignore into formula_token "
                ):
                    in_insert_block = True
                    while j < n and lines[j].strip() != ";":
                        drop.add(j)
                        j += 1
                    if j < n:
                        drop.add(j)  # terminator `;`
                        j += 1
                    in_insert_block = False
                    continue
                if in_insert_block:
                    j += 1
                    continue
                if cur.strip() == "" or (
                    cur.startswith("--")
                    and (
                        "Formulas migrated from" in cur
                        or "via build_create_sql" in cur
                        or cur.rstrip().endswith("=" * 10)
                        or cur.rstrip().endswith("=" * 20)
                    )
                ):
                    drop.add(j)
                    j += 1
                    continue
                break
            i = j
            continue
        i += 1

    return "".join(line for idx, line in enumerate(lines) if idx not in drop)


def main() -> int:
    if not SEED_PATH.exists():
        print(f"missing {SEED_PATH}", file=sys.stderr)
        return 1
    if not FORMULAS_DIR.is_dir():
        print(f"missing {FORMULAS_DIR}", file=sys.stderr)
        return 1
    if not DB_PATH.exists():
        print(f"missing {DB_PATH}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    yaml_paths = sorted(FORMULAS_DIR.glob("*.yaml"))
    print(f"migrating {len(yaml_paths)} yaml files...", file=sys.stderr)

    # Each entry is (yaml_id, formula_sql, token_sql).  yaml_id is
    # also the formula_id that ends up in seed.sql — the migration
    # enforces filename == yaml id == seed.sql formula_id, so we
    # don't need any collision handling: the yaml authoring contract
    # is that those three are the same string, and we reject yamls
    # whose `id:` doesn't match their filename in process_yaml().
    formula_sql_blocks: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    for p in yaml_paths:
        try:
            result = process_yaml(conn, p)
        except MigrationError as e:
            print(f"error: {e}", file=sys.stderr)
            print("migration aborted; seed.sql left untouched",
                  file=sys.stderr)
            return 1
        stem, yaml_id, f_sql, t_sql = result
        if yaml_id in seen_ids:
            # Should be unreachable given process_yaml()'s checks,
            # but defend against future changes (e.g. a symlink that
            # points two filenames at the same yaml content).
            print(
                f"error: {p.name}: duplicate yaml id {yaml_id!r} "
                f"(already emitted; check the yaml directory for duplicates)",
                file=sys.stderr,
            )
            return 1
        seen_ids.add(yaml_id)
        formula_sql_blocks.append((yaml_id, f_sql, t_sql))

    # Sort by yaml id (case-insensitive).  The token rows ride along
    # with their parent formula so a stable sort here also groups all
    # tokens for one formula together.
    formula_sql_blocks.sort(key=lambda r: r[0].casefold())

    print(
        f"  ok={len(formula_sql_blocks)}",
        file=sys.stderr,
    )

    seed_head = split_seed(SEED_PATH.read_text())

    # Build the new formula/formula_token blocks.  Use multi-row VALUES
    # blocks to match the existing seed.sql style.  The token rows for
    # each formula already come out in position order from
    # build_create_sql, so we just concatenate them in the same order
    # the formulas appear above.
    formula_rows = []
    token_rows = []
    for _yaml_id, f_sql, t_sql in formula_sql_blocks:
        # Strip the leading "INSERT OR IGNORE INTO formula (id, name, ...)"
        # header + the closing `;`; we want only the row tuple(s).
        m = re.search(r"VALUES\s*(.+?);\s*$", f_sql, re.DOTALL)
        if not m:
            continue
        rows_str = m.group(1).strip()
        # The /create algorithm emits exactly one row per formula.
        formula_rows.append(f"  {rows_str}")
        m = re.search(r"VALUES\s*(.+?);\s*$", t_sql, re.DOTALL)
        if not m:
            continue
        rows_str = m.group(1).strip()
        for row in rows_str.splitlines():
            row = row.strip().rstrip(",")
            if not row:
                continue
            token_rows.append(f"  {row}")

    new_blocks = []
    if formula_rows:
        new_blocks.append(
            "-- ============================================================\n"
            "-- Formulas migrated from /home/admin/equations/formulas/\n"
            "-- via build_create_sql (mirrors Scifind's /create page).\n"
            "-- ============================================================\n"
            "INSERT OR IGNORE INTO formula (id, name, topic, difficulty, description, links) VALUES\n"
            + ",\n".join(formula_rows)
            + ";\n"
        )
    if token_rows:
        new_blocks.append(
            "INSERT OR IGNORE INTO formula_token\n"
            "  (formula_id, position, token_kind, quantity_id, constant_id,\n"
            "   operator_id, value, label, name_overwrite, symbol_overwrite)\n"
            "VALUES\n"
            + ",\n".join(token_rows)
            + ";\n"
        )

    new_seed = seed_head.rstrip() + "\n\n" + "\n\n".join(new_blocks) + "\n"

    # Rotate the previous seed to seed.sql.bak — only after every yaml
    # processed cleanly, so crashed runs don't leave backups behind.
    # If a .bak already exists from an earlier run we don't clobber it —
    # name the new copy seed.sql.bak.<UTC-timestamp> instead so both
    # copies survive.
    if SEED_BAK_PATH.exists():
        from datetime import datetime, timezone
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = SEED_BAK_PATH.with_name(f"{SEED_BAK_PATH.name}.{stamp}")
        shutil.copy2(SEED_PATH, backup)
        print(f"preserved existing {SEED_BAK_PATH.name} at {backup.name}", file=sys.stderr)
    else:
        shutil.copy2(SEED_PATH, SEED_BAK_PATH)
        print(f"saved backup as {SEED_BAK_PATH.name}", file=sys.stderr)

    SEED_PATH.write_text(new_seed)
    print(f"wrote {SEED_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
