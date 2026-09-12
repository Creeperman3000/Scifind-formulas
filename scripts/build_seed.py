#!/usr/bin/env python3
"""Migrate /home/admin/Projects/Scifind-formulas/formulas/*.yaml to seed.sql.

Mirrors the algorithm used by Scifind's /create page
(`build_create_sql` in scifind_lib.py). The migration replaces the existing
formula / formula_token blocks in seed.sql with the result of running every
yaml file through that pipeline.

Run from any CWD; the script reads from /home/admin/Projects/Scifind-formulas/formulas/
and rewrites /home/admin/Projects/Scifind/seed.sql in place (after stashing
the previous copy at seed.sql.bak next to it — but only once every yaml
has processed cleanly).

Usage:
    python3 scripts/build_seed.py

Exit codes:
    0  on success
    1  on any problem: a broken/unparseable yaml, missing fields, an id
        that doesn't match its filename, an equation referencing an
        unknown identifier, or a filesystem failure that prevents the
        migration from completing. Every offending file is reported by
        name before the run aborts; seed.sql is left untouched. Broken
        files are never silently skipped.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scifind_lib  # noqa: E402
from yaml_overrides import build_overrides  # noqa: E402

FORMULAS_DIR = Path("/home/admin/Projects/Scifind-formulas/formulas")
SEED_PATH = SCIFIND_DIR / "seed.sql"
SEED_BAK_PATH = SCIFIND_DIR / "seed.sql.bak"
DB_PATH = SCIFIND_DIR / "scifind.db"
MAX_OLD_BACKUPS = 5


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
    if not isinstance(name, str) or not isinstance(topic, str) \
            or not isinstance(equation, str) or not isinstance(description, str):
        raise MigrationError(
            f"{yaml_path.name}: name/topic/equation/description must be strings")
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
    if not isinstance(difficulty, int) or isinstance(difficulty, bool):
        try:
            difficulty = int(difficulty)
        except (ValueError, TypeError):
            raise MigrationError(
                f"{yaml_path.name}: difficulty {difficulty!r} is not an integer")

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
    if raw_links is None:
        links = None
    elif isinstance(raw_links, list):
        bad = [type(u).__name__ for u in raw_links if not isinstance(u, str)]
        if bad:
            raise MigrationError(
                f"{yaml_path.name}: `links` must be a list of strings; "
                f"found {bad}")
        links = [u.strip() for u in raw_links if u.strip()]
    else:
        raise MigrationError(
            f"{yaml_path.name}: `links` must be a list, got "
            f"{type(raw_links).__name__}")

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
            formula_id=yaml_id,
        )
    except Exception as e:
        raise MigrationError(
            f"{yaml_path.name}: build_create_sql failed: {e!r}") from e

    # `formula_id=yaml_id` above guarantees filename, yaml id, and SQL
    # formula_id are the same string, so no post-processing of the SQL
    # is needed.
    return stem, yaml_id, f_sql, t_sql


def _extract_values_body(sql: str) -> str | None:
    """Return the comma-separated row tuples that follow a ``VALUES`` keyword.

    Walks the SQL character-by-character after `VALUES` to find the closing
    `;` while respecting single-quoted string literals (where `;` inside
    them is data, not a terminator). Returns None if the structure isn't a
    well-formed `VALUES ... ;` block.
    """
    m = re.search(r"\bVALUES\b", sql, re.IGNORECASE)
    if not m:
        return None
    i = m.end()
    n = len(sql)
    in_quote = False
    while i < n:
        c = sql[i]
        if c == "'":
            # Doubled '' inside a string literal is an escaped quote, not a close.
            if in_quote and i + 1 < n and sql[i + 1] == "'":
                i += 2
                continue
            in_quote = not in_quote
        elif c == ";" and not in_quote:
            return sql[m.end():i].strip()
        i += 1
    return None


def _split_value_rows(body: str) -> list[str]:
    """Split a `VALUES` body into one trimmed row tuple per formula token.

    Walks the body looking for top-level `(` that starts a row tuple and
    matches its closing `)`, again respecting single-quoted string literals.
    """
    rows: list[str] = []
    n = len(body)
    i = 0
    while i < n:
        if body[i] != "(":
            i += 1
            continue
        depth = 0
        in_quote = False
        j = i
        while j < n:
            c = body[j]
            if c == "'":
                if in_quote and j + 1 < n and body[j + 1] == "'":
                    j += 2
                    continue
                in_quote = not in_quote
            elif not in_quote:
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        rows.append(body[i:j + 1].strip())
                        i = j + 1
                        break
            j += 1
        else:
            break
        i = max(i, j + 1)
    return rows


def _is_migration_insert(cur: str) -> bool:
    """True if `cur` starts an INSERT into a table this migration owns.

    Matches ``formula``, ``formula_token``, ``formula_relation``, and
    ``formula_relations`` (the legacy plural), requiring a space, newline,
    or open paren after the table name so ``formula_token_xyz`` would not
    match.
    """
    s = cur.lstrip().lower()
    if not s.startswith("insert or ignore into "):
        return False
    rest = s[len("insert or ignore into "):]
    for table in ("formula ", "formula_token", "formula_relation",
                  "formula_relations"):
        if not rest.startswith(table):
            continue
        # Peek the next character; if it's space, '(', or newline the line
        # is a well-formed INSERT start for this table. Don't .lstrip()
        # here — a newline at end-of-line means "header continues on the
        # next line" (column list), which is the normal layout.
        if len(rest) == len(table):
            nxt = ""
        else:
            nxt = rest[len(table)]
        if nxt in "(\n ":
            return True
    return False


def _statement_end(lines: list[str], start: int) -> int:
    """Return the index of the line holding the `;` that terminates the
    SQL statement starting at line *start*.

    Scans quote-aware (respecting single-quoted literals and ``''``
    escapes) so a `;` inside a description or name string is data, not
    a terminator. Handles both the current seed.sql style, where the
    final row ends with ``...);``, and the older style with a bare
    ``;`` on its own line. Falls back to the last line if no
    terminator is found.
    """
    in_quote = False
    n = len(lines)
    j = start
    while j < n:
        line = lines[j]
        k = 0
        while k < len(line):
            c = line[k]
            if c == "'":
                if in_quote and k + 1 < len(line) and line[k + 1] == "'":
                    k += 2
                    continue
                in_quote = not in_quote
            elif c == ";" and not in_quote:
                return j
            k += 1
        j += 1
    return n - 1


def split_seed(seed_text: str) -> str:
    """Strip every existing formula/formula_token/formula_relation INSERT block
    plus the comment header banner that marks a previous migration's output.

    Returns ``seed_text`` with those blocks removed. The migration replaces
    the formulas wholesale, so the old formula rows, their tokens, and the
    relations (which all key off the old formula ids) must all go. Stripping
    the migration banner lets the script be idempotent across multiple runs.

    Algorithm: single left-to-right scan over the seed's lines. When we see
    the start of an INSERT into one of the targeted tables, drop every line
    up to and including its closing ``;``. When we see the first line of a
    migration banner (a line starting with ``--`` whose content matches one
    of the banner markers), drop the banner and any continuation lines that
    are themselves banner markers, blank lines, or INSERT-marker lines,
    until the next non-banner content.
    """
    lines = seed_text.splitlines(keepends=True)
    drop: set[int] = set()

    banner_match = (
        lambda cur: cur.startswith("--") and (
            "Formulas migrated from" in cur
            or "via build_create_sql" in cur
            or cur.rstrip().endswith("=" * 10)
            or cur.rstrip().endswith("=" * 20)
        )
    )

    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]

        # Targeted INSERT blocks: drop from this line through the line
        # holding the statement-terminating `;`.
        if _is_migration_insert(line):
            j = _statement_end(lines, i)
            for k in range(i, j + 1):
                drop.add(k)
            i = j + 1
            continue

        # Banner block: drop this line plus any further banner-marker / blank /
        # INSERT-marker lines until we hit unrelated content. If the banner
        # is followed by an INSERT block, drop that INSERT too.
        if banner_match(line):
            j = i
            while j < n:
                cur = lines[j]
                if _is_migration_insert(cur):
                    end = _statement_end(lines, j)
                    for k in range(j, end + 1):
                        drop.add(k)
                    j = end + 1
                    continue
                if cur.strip() == "" or banner_match(cur):
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
    #
    # Two phases: first process every yaml, collecting per-file errors,
    # so a run with several broken files reports all of them at once
    # instead of forcing fix-and-rerun cycles. Only when every yaml is
    # clean do we touch seed.sql — a failed run never leaves a partial
    # migration behind, and broken files are never silently skipped.
    formula_sql_blocks: list[tuple[str, str, str]] = []
    failures: list[str] = []
    seen_ids: set[str] = set()
    for p in yaml_paths:
        try:
            result = process_yaml(conn, p)
        except MigrationError as e:
            failures.append(str(e))
            continue
        stem, yaml_id, f_sql, t_sql = result
        if yaml_id in seen_ids:
            # Should be unreachable given process_yaml()'s checks,
            # but defend against future changes (e.g. a symlink that
            # points two filenames at the same yaml content).
            failures.append(
                f"{p.name}: duplicate yaml id {yaml_id!r} "
                f"(already emitted; check the yaml directory for duplicates)"
            )
            continue
        seen_ids.add(yaml_id)
        formula_sql_blocks.append((yaml_id, f_sql, t_sql))

    if failures:
        for msg in failures:
            print(f"error: {msg}", file=sys.stderr)
        print(f"migration aborted ({len(failures)} broken "
              f"file{'s' if len(failures) != 1 else ''}); "
              f"seed.sql left untouched", file=sys.stderr)
        return 1

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
        # Extract the row tuple(s) — everything between the VALUES keyword
        # and the closing `;`. Each row is parenthesised SQL; we anchor on
        # balanced parens so an embedded `;` inside a string literal (which
        # sql_literal does NOT escape) doesn't truncate the match early.
        f_body = _extract_values_body(f_sql)
        if f_body is not None:
            formula_rows.append(f"  {f_body}")
        t_body = _extract_values_body(t_sql)
        if t_body is not None:
            for row in _split_value_rows(t_body):
                token_rows.append(f"  {row}")

    new_blocks = []
    if formula_rows:
        new_blocks.append(
            "-- ============================================================\n"
            "-- Formulas migrated from Scifind-formulas/formulas/\n"
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
            "   operator_id, value, symbol_overwrite, name_overwrite)\n"
            "VALUES\n"
            + ",\n".join(token_rows)
            + ";\n"
        )

    new_seed = seed_head.rstrip() + "\n\n" + "\n\n".join(new_blocks) + "\n"

    # Rotate the previous seed to seed.sql.bak — only after every yaml
    # processed cleanly, so crashed runs don't leave backups behind. The
    # previous run's `seed.sql.bak` (which holds the state before *that*
    # run) is preserved as a timestamped copy so it survives this run's
    # rotation. Cap the number of timestamped backups we keep around so
    # repeated runs don't accumulate files indefinitely.
    from datetime import datetime, timezone
    if SEED_BAK_PATH.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        prev = SEED_BAK_PATH.with_name(f"{SEED_BAK_PATH.name}.{stamp}")
        shutil.copy2(SEED_BAK_PATH, prev)
        print(f"preserved prior backup as {prev.name}", file=sys.stderr)
        # Trim older timestamped backups, keep at most MAX_OLD_BACKUPS.
        siblings = sorted(SEED_BAK_PATH.parent.glob(f"{SEED_BAK_PATH.name}.*"))
        for old in siblings[: max(0, len(siblings) - MAX_OLD_BACKUPS)]:
            old.unlink()
    shutil.copy2(SEED_PATH, SEED_BAK_PATH)
    print(f"saved current seed as {SEED_BAK_PATH.name}", file=sys.stderr)

    SEED_PATH.write_text(new_seed)
    print(f"wrote {SEED_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
