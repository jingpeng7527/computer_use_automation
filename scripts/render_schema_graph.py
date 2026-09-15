"""Render the Pydantic contracts as reviewable schema diagrams.

The diagrams deliberately distinguish two meanings that look similar in an
artifact JSON file:

* solid edges are Pydantic ownership/type edges, derived from JSON Schema
  ``$ref`` values;
* dashed edges are ID or template references which the artifact validator
  checks across fields (for example ``OutputSpec.source_step_id -> Step.id``).

The SVG, DOT, and JSON Schema files under ``docs/schema`` are generated
artifacts.  Do not edit them by hand; re-run this script after changing a
Pydantic model or its cross-field contract.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from pydantic import BaseModel

from cua.schema import Capability, Overlay, ReplayResult

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = REPO_ROOT / "docs" / "schema"


@dataclass(frozen=True)
class DiagramSpec:
    name: str
    title: str
    model: type[BaseModel]
    clusters: dict[str, tuple[str, ...]]


ARTIFACT = DiagramSpec(
    name="artifact-contract",
    title="Capability artifact contract",
    model=Capability,
    clusters={
        "Artifact API": (
            "Capability",
            "AppProfile",
            "ParamSpec",
            "OutputSpec",
            "Provenance",
            "RuntimeMatch",
            "RecoveryAction",
            "RecoveryBudget",
        ),
        "Procedure": (
            "Step",
            "Navigate",
            "Click",
            "TypeText",
            "Select",
            "Read",
            "Wait",
            "WaitSpec",
        ),
        "Target resolution": (
            "Target",
            "RoleNameStrategy",
            "LabelAnchorStrategy",
            "CssStrategy",
            "BboxStrategy",
        ),
        "Verification": (
            "Checkpoint",
            "TextContains",
            "RoleName",
            "UrlMatches",
            "ValueEquals",
            "NamedPredicate",
            "TextMatcher",
        ),
    },
)

REPLAY_RESULT = DiagramSpec(
    name="replay-result-contract",
    title="Replay result contract",
    model=ReplayResult,
    clusters={
        "Caller-facing result": ("ReplayResult",),
        "Trace and typed values": ("StepResult", "Money"),
        "Terminal payloads": ("BusinessOutcomeResult", "FailureDetail", "EscalationRef"),
    },
)

OVERLAY = DiagramSpec(
    name="overlay-contract",
    title="Tenant overlay contract",
    model=Overlay,
    clusters={
        "Overlay": ("Overlay",),
        "Override operations": ("ReplaceTarget", "InsertAfter", "Skip"),
        "Target resolution": (
            "Target",
            "RoleNameStrategy",
            "LabelAnchorStrategy",
            "CssStrategy",
            "BboxStrategy",
        ),
    },
)

SPECS = (ARTIFACT, REPLAY_RESULT, OVERLAY)


def _dot_id(value: str) -> str:
    """Return a Graphviz-safe quoted identifier."""
    return json.dumps(value)


def _ref_name(ref: str) -> str | None:
    prefix = "#/$defs/"
    return ref.removeprefix(prefix) if ref.startswith(prefix) else None


def _refs(value: Any) -> set[str]:
    """Collect model names from every JSON-Schema reference below *value*."""
    if isinstance(value, dict):
        refs = {_ref_name(value["$ref"])} if "$ref" in value else set()
        return refs | set().union(*(_refs(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_refs(item) for item in value)) if value else set()
    return set()


def _field_kind(field: dict[str, Any]) -> str:
    """Return a compact, reader-facing type summary for a JSON-Schema field."""
    refs = sorted(ref for ref in _refs(field) if ref)
    if field.get("type") == "array":
        item_refs = sorted(ref for ref in _refs(field.get("items", {})) if ref)
        return f"list[{', '.join(item_refs) if item_refs else 'scalar'}]"
    if refs:
        return " | ".join(refs) if len(refs) <= 3 else f"union[{len(refs)}]"
    if "enum" in field:
        return "enum"
    return str(field.get("type", "scalar"))


def _node_label(model_name: str, definition: dict[str, Any]) -> str:
    """Create an HTML-like Graphviz label listing a model's public fields."""
    properties = definition.get("properties", {})
    required = set(definition.get("required", []))
    rows = [f'<TR><TD BGCOLOR="#e8eef7"><B>{escape(model_name)}</B></TD></TR>']
    for field_name, field in properties.items():
        marker = "" if field_name in required else "?"
        text = f"{field_name}{marker}: {_field_kind(field)}"
        rows.append(f'<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="10">{escape(text)}</FONT></TD></TR>')
    if not properties:
        rows.append('<TR><TD ALIGN="LEFT"><FONT POINT-SIZE="10">scalar / alias</FONT></TD></TR>')
    return '<<TABLE BGCOLOR="#ffffff" BORDER="0" CELLBORDER="1" CELLSPACING="0" CELLPADDING="5">{}</TABLE>>'.format(
        "".join(rows)
    )


def _cluster_for(spec: DiagramSpec, model_name: str) -> str | None:
    for title, model_names in spec.clusters.items():
        if model_name in model_names:
            return title
    return None


def _schema_dot(spec: DiagramSpec) -> str:
    """Render model containment links directly from Pydantic's JSON Schema."""
    schema = spec.model.model_json_schema()
    definitions = dict(schema.get("$defs", {}))
    definitions[spec.model.__name__] = {
        "properties": schema.get("properties", {}),
        "required": schema.get("required", []),
    }

    clustered: dict[str, list[str]] = {}
    uncategorized: list[str] = []
    for model_name in sorted(definitions):
        cluster = _cluster_for(spec, model_name)
        if cluster is None:
            uncategorized.append(model_name)
            continue
        clustered.setdefault(cluster, []).append(model_name)
    if uncategorized:
        # A silent "Other" bucket would mean a model added to the schema
        # and never added to DiagramSpec.clusters just quietly renders
        # miscategorized -- the diagram stays "renderable" but stops being
        # "meaningful" with nothing to say so. Failing here turns that into
        # a loud, specific fix instead of a diagram nobody double-checks.
        raise ValueError(
            f"{spec.name}: no cluster claims {uncategorized} -- add each to a "
            f"DiagramSpec.clusters entry (or a new one) in this script."
        )

    lines = [
        "digraph schema {",
        '  graph [rankdir=TB, bgcolor="#ffffff", pad="0.25", nodesep="0.45", ranksep="1.0", fontname="Helvetica"];',
        '  node [shape=plain, fontname="Helvetica"];',
        '  edge [color="#425466", fontname="Helvetica", fontsize=9, arrowsize=0.7];',
        f'  labelloc="t"; label={json.dumps(spec.title)}; fontsize=20; fontname="Helvetica";',
    ]

    for index, (cluster, model_names) in enumerate(clustered.items()):
        lines.extend(
            [
                f"  subgraph cluster_{index} {{",
                f'    label={json.dumps(cluster)}; color="#b9c6d8"; penwidth=1; style="rounded";',
            ]
        )
        for model_name in model_names:
            lines.append(
                f"    {_dot_id(model_name)} [label={_node_label(model_name, definitions[model_name])}];"
            )
        lines.append("  }")

    emitted_edges: set[tuple[str, str, str]] = set()
    for model_name, definition in sorted(definitions.items()):
        for field_name, field in definition.get("properties", {}).items():
            for ref in sorted(ref for ref in _refs(field) if ref in definitions):
                edge = (model_name, ref, field_name)
                if edge not in emitted_edges:
                    lines.append(
                        f"  {_dot_id(model_name)} -> {_dot_id(ref)} [label={json.dumps(field_name)}];"
                    )
                    emitted_edges.add(edge)

    lines.append("}")
    return "\n".join(lines) + "\n"


def _semantic_references_dot() -> str:
    """Draw references which JSON Schema cannot infer from string fields alone."""
    nodes = {
        "Capability.inputs": "Capability.inputs\nParamSpec.name",
        "Capability.outputs": "Capability.outputs\nOutputSpec.name",
        "Step.id": "Step.id",
        "RuntimeMatch.after_step": "RuntimeMatch.after_step",
        "OutputSpec.source_step_id": "OutputSpec.source_step_id",
        "TypeText.value_from": "TypeText.value_from\n{{input.x}} | {{ctx.x}}",
        "Select.value_from": "Select.value_from\n{{input.x}} | {{ctx.x}}",
        "Read.into": "Read.into\noutput_name | ctx.x",
        "Overlay.extends": "Overlay.extends\ncapability_id@version",
        "Overlay.step_id": "Overlay override step_id",
        "Capability.version": "Capability\ncapability_id@version",
    }
    edges = (
        ("OutputSpec.source_step_id", "Step.id", "produced by"),
        ("RuntimeMatch.after_step", "Step.id", "appears after"),
        ("TypeText.value_from", "Capability.inputs", "input binding"),
        ("Select.value_from", "Capability.inputs", "input binding"),
        ("Read.into", "Capability.outputs", "declared output"),
        ("Read.into", "TypeText.value_from", "ctx producer -> consumer"),
        ("Read.into", "Select.value_from", "ctx producer -> consumer"),
        ("Overlay.extends", "Capability.version", "base artifact"),
        ("Overlay.step_id", "Step.id", "patches"),
    )
    lines = [
        "digraph semantic_references {",
        '  graph [rankdir=LR, bgcolor="#ffffff", pad="0.25", nodesep="0.55", ranksep="1.1", fontname="Helvetica"];',
        '  node [shape=box, style="rounded,filled", color="#7e8ea3", fillcolor="#f5f8fc", fontname="Helvetica", fontsize=11, margin="0.16,0.10"];',
        '  edge [style=dashed, color="#7c3aed", fontname="Helvetica", fontsize=9, arrowsize=0.7];',
        '  labelloc="t"; label="Semantic references in the contract"; fontsize=20; fontname="Helvetica";',
        '  legend [shape=plaintext, label="dashed arrow = ID or template relationship; solid arrows appear in the type diagrams"];',
    ]
    for node_id, label in nodes.items():
        lines.append(f"  {_dot_id(node_id)} [label={json.dumps(label)}];")
    for source, destination, label in edges:
        lines.append(f"  {_dot_id(source)} -> {_dot_id(destination)} [label={json.dumps(label)}];")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _documents() -> dict[str, str]:
    """Return every generated text document, keyed by its relative filename."""
    documents: dict[str, str] = {}
    for spec in SPECS:
        schema = spec.model.model_json_schema()
        documents[f"{spec.name}.schema.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
        documents[f"{spec.name}.dot"] = _schema_dot(spec)
    documents["semantic-references.dot"] = _semantic_references_dot()
    return documents


def _render_svg(dot_path: Path, svg_path: Path) -> None:
    dot = shutil.which("dot")
    if dot is None:
        raise RuntimeError("Graphviz 'dot' is required; install it with 'brew install graphviz'.")
    subprocess.run([dot, "-Tsvg", str(dot_path), "-o", str(svg_path)], check=True)
    ElementTree.parse(svg_path)  # fail if a renderer ever emits malformed XML


def render(output_dir: Path) -> None:
    """Write JSON Schema, DOT sources, and their rendered SVGs."""
    output_dir.mkdir(parents=True, exist_ok=True)
    for relative_path, content in _documents().items():
        path = output_dir / relative_path
        path.write_text(content, encoding="utf-8")
        if path.suffix == ".dot":
            _render_svg(path, path.with_suffix(".svg"))


def check(output_dir: Path) -> bool:
    """Return whether committed generated documentation matches the models."""
    with tempfile.TemporaryDirectory() as temp_dir:
        expected_dir = Path(temp_dir)
        render(expected_dir)
        expected_names = {path.name for path in expected_dir.iterdir()}
        actual_names = (
            {path.name for path in output_dir.iterdir() if path.name != "README.md"}
            if output_dir.exists()
            else set()
        )
        if expected_names != actual_names:
            print(
                f"schema diagram files differ: expected {sorted(expected_names)}, found {sorted(actual_names)}"
            )
            return False
        stale = [
            name
            for name in sorted(expected_names)
            if (expected_dir / name).read_bytes() != (output_dir / name).read_bytes()
        ]
        if stale:
            print(f"schema diagram files are stale: {', '.join(stale)}")
            return False
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when committed diagrams differ from generated output",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        return 0 if check(args.output_dir) else 1
    render(args.output_dir)
    print(f"rendered schema diagrams in {args.output_dir.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
