"""Translate a yaml formula's override lists into the override dicts the
Scifind APIs expect.

The yaml authoring format stores overrides as two parallel arrays:

    symbol_overrides:
      - {<quantity_id>: <value>}   # 1st entry
      - {<quantity_id>: <value>}   # 2nd entry
      ...
    name_overrides:
      - {<quantity_id>: <value>}   # 1st entry
      ...

Entries are consumed left-to-right across occurrences of the *same*
quantity_id, with empty-string values acting as padding that advances
the cursor without writing anything. The two arrays maintain independent
cursors, matching the documented yaml semantics.

Both Scifind APIs (`build_create_sql` and `parse_and_preview_equation`)
key their overrides by ``{qid}|{label}|{pos}`` where ``label`` is the
token's existing alias (empty string when unlabelled). The override
dict at each key carries ``symbol`` and ``name_overwrite`` fields.
This helper emits that shape.

Override entries that reference a quantity id absent from the equation
are silently dropped (the per-qid cursor never advances past the missing
target). The caller is expected to surface this — `audit.py` does via
the ``overrides`` check; the build/render scripts use the warnings
written to stderr by this module.
"""
from __future__ import annotations

import sys
from typing import Any

from scifind_lib.parser import parse_equation


def build_overrides(
    conn,
    equation: str,
    sym_ov: Any,
    nm_ov: Any,
) -> dict[str, dict[str, str]]:
    """Translate yaml's position-indexed override lists into the
    ``{<qid>|<label>|<pos>: {symbol, name_overwrite}}`` dict that
    ``build_create_sql`` and ``parse_and_preview_equation`` both
    consume.

    Empty-string values advance the per-qid cursor without writing
    anything, so an entry like ``{m: ""}`` reserves the slot for the
    next ``m`` occurrence and lets the next non-empty entry land on a
    later occurrence. The two lists (symbol_overrides, name_overrides)
    maintain independent per-qid cursors so an empty-string padding
    in one does not skip a slot in the other.

    Phantom qids (referenced by an override entry but absent from the
    equation) are dropped without writing any output and produce a
    stderr warning. Multiple entries for the same phantom qid emit
    one warning each so authors can spot the full list.
    """
    overrides: dict[str, dict[str, str]] = {}
    if not (sym_ov or nm_ov):
        return overrides
    tokens = parse_equation(conn, equation)

    occ: dict[str, list[tuple[str, int]]] = {}
    for pos, tok in enumerate(tokens, start=1):
        if tok.get("token_kind") == "quantity":
            occ.setdefault(tok["quantity_id"], []).append(
                ((tok.get("label") or ""), pos)
            )

    known_qids = set(occ)

    def advance(
        qid: str,
        val: Any,
        state: dict[str, int],
        field: str,
    ) -> None:
        if val is None:
            val = ""
        val = str(val)
        if qid not in known_qids:
            print(
                f"yaml_overrides: ignoring override for unknown quantity "
                f"{qid!r} (not present in equation {equation!r})",
                file=sys.stderr,
            )
            return
        positions = occ.get(qid, [])
        cursor = state.get(qid, 0)
        if cursor >= len(positions):
            return
        token_label, actual_pos = positions[cursor]
        state[qid] = cursor + 1
        if val == "":
            return
        key = f"{qid}|{token_label}|{actual_pos}"
        overrides.setdefault(key, {})[field] = val

    sym_state: dict[str, int] = {}
    nm_state: dict[str, int] = {}
    for ov_list, state in (
        (sym_ov, sym_state),
        (nm_ov, nm_state),
    ):
        if not isinstance(ov_list, list):
            if ov_list is not None:
                print(
                    f"yaml_overrides: ignoring non-list override entry "
                    f"{ov_list!r}",
                    file=sys.stderr,
                )
            continue
        for ov in ov_list:
            if not isinstance(ov, dict):
                print(
                    f"yaml_overrides: ignoring non-mapping override entry "
                    f"{ov!r}",
                    file=sys.stderr,
                )
                continue
            for qid, val in ov.items():
                advance(qid, val, state, "symbol" if state is sym_state else "name_overwrite")
    return overrides