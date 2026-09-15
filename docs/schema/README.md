# Schema diagrams

These files are generated from the Pydantic contracts, not hand-maintained
architecture sketches.

- **Solid arrows** in `artifact-contract.svg`, `replay-result-contract.svg`, and
  `overlay-contract.svg` mean a field owns or selects a typed model. They are
  inferred from the models' generated JSON Schema `$ref`s.
- **Dashed arrows** in `semantic-references.svg` mean a string ID or template
  reference. `Capability` validates its artifact relations across fields; the
  overlay links document relations that will be checked when that stretch is
  applied. JSON Schema cannot infer either kind from a `str` annotation alone.

Regenerate all artifacts after changing a schema model:

```bash
brew install graphviz # macOS, once
.venv/bin/python scripts/render_schema_graph.py
.venv/bin/python scripts/render_schema_graph.py --check
```

The script also writes the exact JSON Schema and Graphviz DOT source beside each
SVG. This makes changes reviewable as text, while SVG gives reviewers a compact
overview.

`tests/test_schema_diagrams.py` also calls `--check`'s underlying function, so a
stale diagram fails `pytest`, not just a manual `--check` run -- but nothing in
this repo runs `pytest` automatically on a schema change (there's no CI wired up
under `.github/workflows/`). Regenerating is still a step a person has to
remember, not one that's enforced; `pytest` only catches it after the fact, next
time someone happens to run the suite.
