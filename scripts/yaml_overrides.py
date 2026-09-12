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
dict at each key carries ``symbol`` and ``name`` fields — the same
shape the /create page posts (see `_parse_override_form_keys` in
Scifind's webapp.py). This helper emits that shape.

Override entries that reference a quantity id absent from the equation
are silently dropped (the per-qid cursor never advances past the missing
target). The caller is expected to surface this — `audit.py` does via
the ``overrides`` check; the build/render scripts use the warnings
written to stderr by this module.

Entries that reference a *constant* present in the equation (e.g.
``standard_gravity``, which Scifind moved from the quantity to the
constant table) are likewise dropped, with their own warning: neither
``build_create_sql`` nor ``parse_and_preview_equation`` consumes
overrides for constant tokens — constants always render with their
fixed symbol — so such entries can never take effect and should be
removed from the yaml.
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
    ``{<qid>|<label>|<pos>: {symbol, name}}`` dict that
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
    one warning each so authors can spot the full list. Entries naming
    a constant present in the equation are also dropped — with a
    dedicated warning — because Scifind takes no overrides for
    constant tokens.
    """
    overrides: dict[str, dict[str, str]] = {}
    if not (sym_ov or nm_ov):
        return overrides
    tokens = parse_equation(conn, equation)

    occ: dict[str, list[tuple[str, int]]] = {}
    const_ids: set[str] = set()
    for pos, tok in enumerate(tokens, start=1):
        if tok.get("token_kind") == "quantity":
            occ.setdefault(tok["quantity_id"], []).append(
                ((tok.get("label") or ""), pos)
            )
        elif tok.get("token_kind") == "constant" and tok.get("constant_id"):
            const_ids.add(tok["constant_id"])

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
            if qid in const_ids:
                print(
                    f"yaml_overrides: ignoring override for constant "
                    f"{qid!r} (present in equation {equation!r}, but "
                    f"Scifind takes no overrides for constant tokens)",
                    file=sys.stderr,
                )
            else:
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
                advance(qid, val, state, "symbol" if state is sym_state else "name")
    return overrides
