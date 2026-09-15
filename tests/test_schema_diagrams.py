"""Generated schema documentation must remain renderable and meaningful."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path
from xml.etree import ElementTree

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_schema_graph.py"
COMMITTED_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "docs" / "schema"


def _load_renderer_module():
    spec = importlib.util.spec_from_file_location("render_schema_graph", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(shutil.which("dot") is None, reason="Graphviz dot is not installed")
def test_schema_diagrams_render_from_current_pydantic_models(tmp_path: Path) -> None:
    renderer = _load_renderer_module()
    renderer.render(tmp_path)
    assert renderer.check(COMMITTED_OUTPUT_DIR), "run scripts/render_schema_graph.py"

    # Iterate the script's own SPECS rather than a hand-copied name->model
    # mapping here -- a duplicated list silently stops covering a spec the
    # moment someone adds a fourth DiagramSpec to the script and forgets to
    # update this test too. This way a new spec is covered for free.
    for spec in renderer.SPECS:
        name = spec.name
        assert (
            json.loads((tmp_path / f"{name}.schema.json").read_text()) == spec.model.model_json_schema()
        )
        assert "digraph schema" in (tmp_path / f"{name}.dot").read_text()
        assert (tmp_path / f"{name}.svg").read_text().startswith("<?xml")
        ElementTree.parse(tmp_path / f"{name}.svg")

    semantic_dot = (tmp_path / "semantic-references.dot").read_text()
    for required_reference in (
        "OutputSpec.source_step_id",
        "RuntimeMatch.after_step",
        "TypeText.value_from",
        "Read.into",
        "Overlay.extends",
    ):
        assert required_reference in semantic_dot
    ElementTree.parse(tmp_path / "semantic-references.svg")
