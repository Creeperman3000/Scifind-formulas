Generate formula YAML files for `/home/admin/equations/`.
Create as many formulas as you can. Make sure there are no duplicates.
Put created files to `formulas/` and name them after their `id`.
Before you start generating, ask questions for clarification. If you need to make an assumption, do so and write it down in `notes.txt`.

- **Quantities, operators, and constants**: live in the Scifind database at
  `/home/admin/Projects/Scifind/scifind.db`. Query it (e.g. `sqlite3
  scifind.db "SELECT id FROM quantity"`) for the canonical vocabulary before
  writing equations. If you genuinely need a quantity/operator/constant that
  isn't there, propose the addition.
- **Topics**: `topic.yaml`

**Format** (see `formulas/pythagorean_theorem.yaml`):
```yaml
name: "Display name"
id: unique_snake_case_id
topic: leaf_topic_from_topic_yaml
difficulty: 3  # 1-10
equation: "(quantity_id operator) (quantity_id quantity_id operator) ..."  # fully parenthesized infix, root is always eq
symbol_overrides:    # symbol_overwrites (optional)
  - quantity_id: "symbol"
name_overrides:    # name_overwrites (optional)
  - quantity_id: "Display override"
```

Note that you can use `quantity_id` multiple times in `symbol_overrides` or `name_overrides`. It will distribute them from left to right  respectively. If the `symbol_overrides` does not target the first occurrence of that `quantity_id`, you can put an empty string as the value for padding.
If you want to and it flows nicely, you can use a link in `name_overrides`. The syntax is `[quantity|optional_text_override]`. Example: `Initial [velocity]`.

**Equation encoding**: Fully parenthesized infix. The root is always `eq` (or `approx`/`prop`). Every operand or sub-expression goes inside `()`. Example for `c^2 = a^2 + b^2`:
```
(length pow 2) eq ((length pow 2) add (length pow 2))
```

See `formulas/pythagorean_theorem.yaml` for the corresponding overrides.

Please note that this is work has already begun, just continue.
