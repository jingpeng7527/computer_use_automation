"""Compiles a successful DiscoveryTranscript into a Capability artifact.

The loop only knows the one node it acted on at each step -- a bare
node_id from that moment's observe(). A reviewable artifact needs a full
RANKED list of plausible strategies for that same element, the way a human
curator would write one by hand: "does it have a real accessible name? If
not, is there stable nearby text to anchor on? Failing that, a stable
class?" This is real reconstruction work, not a formality -- it's what
schema/locator.py's four layers actually get populated from.

Two things a discovery run must never do are enforced here, not assumed:
values are bound by reference (`{{input.<param_name>}}`), never the literal
the model typed, and the raw goal text is templated the same way before it
ever reaches Provenance -- so the literal used to discover this capability
is structurally absent from the artifact, not merely redacted after the
fact.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from cua.schema import (
    AppProfile,
    BboxStrategy,
    Capability,
    Checkpoint,
    Click,
    CssStrategy,
    LabelAnchorStrategy,
    Navigate,
    OutputSpec,
    ParamSpec,
    ParamType,
    Provenance,
    Read,
    RoleName,
    RoleNameStrategy,
    Step,
    Target,
    TypeText,
    UrlMatches,
)
from cua.surface import InteractiveNode, SurfaceSnapshot

from .loop import DiscoveryTranscript, StepLog

_MONEY_RE = re.compile(r"^\$[\d,]+\.\d{2}\s+[A-Z]{3}$")


def _nearest_label_to_the_left(
    node: InteractiveNode, snapshot: SurfaceSnapshot
) -> InteractiveNode | None:
    """A label anchor is only trustworthy when it's the row's ONE static
    descriptive text -- the label/value-pair shape a form or a detail table
    uses ("Member Number:" next to its input; "Savings Balance" next to its
    value). A results-table row has several sibling data cells (a member
    id, a member's name, an action control): "nearest text to the left" in
    that shape isn't a label at all, it's whichever record field happened
    to sit next to this control -- which is exactly how a real member's
    name ended up committed into a versioned, git-tracked artifact before
    this guard existed. Requiring the row to contain exactly one candidate
    turns that ambiguity into "don't anchor here", not "guess"; the caller
    falls through to a css/bbox strategy instead, which is what a stable,
    per-record-independent locator should be built from in that shape."""
    top, bottom = node.bbox.y, node.bbox.y + node.bbox.h
    same_row = [
        n
        for n in snapshot
        if n is not node and not n.interactive and n.text and n.bbox.vertical_overlap(top, bottom)
    ]
    if len(same_row) != 1:
        return None
    candidate = same_row[0]
    return candidate if candidate.bbox.x < node.bbox.x else None


def _strategies_for(node: InteractiveNode, snapshot: SurfaceSnapshot) -> Target:
    """Layer 1 if it has a real accessible name; layer 2 if not, but there's
    stable nearby text to anchor on; layer 3 if it has a stable class;
    layer 4 (bbox) only if nothing else applies at all. Any subset of
    these can coexist -- schema.locator.Target only requires the layers be
    strictly increasing, which building them in this fixed order already
    guarantees."""
    strategies: list = []

    if node.role and node.name:
        strategies.append(
            RoleNameStrategy(
                role=node.role, name=node.name, rationale="has a real accessible name"
            )
        )
    else:
        anchor = _nearest_label_to_the_left(node, snapshot)
        if anchor is not None:
            relation = "same_row_input" if node.interactive else "next_cell"
            strategies.append(
                LabelAnchorStrategy(
                    label=anchor.text,
                    relation=relation,
                    rationale=f"no accessible name at discovery time; anchored on {anchor.text!r}",
                )
            )

    cls = (node.attrs.get("class") or "").split()
    if cls:
        strategies.append(
            CssStrategy(
                selector=f".{cls[0]}",
                rationale="stable, human-authored class name observed at discovery time",
            )
        )

    if not strategies:
        strategies.append(
            BboxStrategy(
                x=node.bbox.x,
                y=node.bbox.y,
                w=node.bbox.w,
                h=node.bbox.h,
                rationale="no role, name, nearby label, or stable class found -- last resort",
            )
        )
    return Target(strategies=strategies)


def _classify_output_type(value: str) -> ParamType:
    return "money" if _MONEY_RE.match(value.strip()) else "string"


def extract_param_literals(steps: list[StepLog]) -> dict[str, str]:
    """Every literal a `type` tool call carried at discovery time, keyed by
    the param_name it's recorded under -- the same mapping used to template
    the goal before it reaches Provenance/summary. Exported so evidence
    written straight from the transcript (cli.py's discover command sees
    every step, not just the ones compile_capability keeps) can redact
    those same literals identically, rather than each caller growing its
    own copy of this logic."""
    literals: dict[str, str] = {}
    for log in steps:
        args = log.tool_call.args
        if log.tool_call.name == "type" and "param_name" in args and "text" in args:
            literals[args["param_name"]] = args["text"]
    return literals


def template_literals(text: str, literals: dict[str, str]) -> str:
    """Replaces every occurrence of a discovery-time literal with the
    `{{input.<name>}}` reference it's bound to -- structural redaction
    (absence by construction) applied to free text, not just to
    Step.action.value_from."""
    for name, literal in literals.items():
        text = text.replace(literal, f"{{{{input.{name}}}}}")
    return text


def compile_capability(
    transcript: DiscoveryTranscript,
    *,
    capability_id: str,
    app_profile: AppProfile,
    discovery_run_id: str,
    transcript_sha256: str,
) -> Capability:
    if not transcript.success:
        raise ValueError(f"cannot compile a failed discovery run: {transcript.reason}")

    real_steps: list[StepLog] = [
        s for s in transcript.steps if s.tool_call.name != "finish" and s.result == "ok"
    ]
    if not real_steps:
        raise ValueError("discovery run finished successfully but took no actions to compile")

    steps: list[Step] = []
    inputs: list[ParamSpec] = []
    outputs: list[OutputSpec] = []
    param_literals = extract_param_literals(real_steps)  # param_name -> literal typed at discovery time

    for i, log in enumerate(real_steps):
        step_id = f"s{i}"
        name = log.tool_call.name
        args = log.tool_call.args

        target = _strategies_for(log.node, log.snapshot) if log.node is not None else None

        # A heading appearing right after this step is evidence we landed
        # somewhere new on purpose -- becomes this step's checkpoint.
        checkpoint = None
        if name in ("navigate", "click") and i + 1 < len(real_steps):
            next_snapshot = real_steps[i + 1].snapshot
            heading = next((n for n in next_snapshot if n.role == "heading" and n.name), None)
            if heading is not None:
                checkpoint = Checkpoint(
                    description=f"reached a page headed {heading.name!r}",
                    all_of=[RoleName(role="heading", name=heading.name)],
                )

        if name == "navigate":
            action = Navigate(url_template=args["url"])
            risk = "SAFE_READ"
            intent = f"navigate to {args['url']}"
        elif name == "click":
            label = (log.node.name or log.node.text or log.node.node_id) if log.node else "?"
            action = Click()
            risk = "REVERSIBLE_WRITE"
            intent = f"click {label!r}"
        elif name == "type":
            param_name = args["param_name"]
            literal = args["text"]
            pattern = f"^[0-9]{{{len(literal)}}}$" if literal.isdigit() else None
            inputs.append(
                ParamSpec(
                    name=param_name,
                    type="string",
                    description=f"captured during discovery as a {param_name} value",
                    pattern=pattern,
                    sensitivity="pii",
                )
            )
            action = TypeText(
                value_from=f"{{{{input.{param_name}}}}}", submit=bool(args.get("submit", False))
            )
            risk = "REVERSIBLE_WRITE"
            intent = f"type {{{{input.{param_name}}}}}" + (" and submit" if args.get("submit") else "")
        elif name == "read":
            output_name = args["output_name"]
            value = transcript.outputs.get(output_name, "")
            outputs.append(
                OutputSpec(
                    name=output_name,
                    type=_classify_output_type(value),
                    description=f"read from the page during discovery ({output_name})",
                    source_step_id=step_id,
                )
            )
            action = Read(into=output_name)
            risk = "SAFE_READ"
            intent = f"read {output_name}"
        else:
            continue  # pragma: no cover -- loop.py never emits any other tool name

        steps.append(
            Step(
                id=step_id,
                intent=intent,
                action=action,
                risk_level=risk,
                target=target,
                checkpoint=checkpoint,
            )
        )

    final_snapshot = real_steps[-1].snapshot
    final_heading = next((n for n in final_snapshot if n.role == "heading" and n.name), None)
    if final_heading is not None:
        success_condition = Checkpoint(
            description=f"final page headed {final_heading.name!r}",
            all_of=[RoleName(role="heading", name=final_heading.name)],
        )
    else:
        success_condition = Checkpoint(
            description="reached the final observed location",
            all_of=[UrlMatches(pattern=f"*{real_steps[-1].location_path}")],
        )

    templated_goal = template_literals(transcript.goal, param_literals)

    return Capability(
        capability_id=capability_id,
        version=1,
        status="draft",
        title=capability_id.rsplit(".", 1)[-1].replace("_", " ").title(),
        summary=templated_goal,
        app_profile=app_profile,
        inputs=inputs,
        outputs=outputs,
        possible_outcomes=[],  # a hardening pass populates these -- a later phase
        steps=steps,
        runtime_matches=[],
        success_condition=success_condition,
        provenance=Provenance(
            discovered_at=datetime.now(UTC),
            model=transcript.model,
            goal=templated_goal,
            discovery_run_id=discovery_run_id,
            transcript_sha256=transcript_sha256,
            # Every entry here has resolution="cleared_obstacle" -- a
            # "workflow_advanced" release sets transcript.success=False
            # (agent/loop.py), which the refusal at the top of this
            # function already raises on before this line is ever reached.
            discovery_handoffs=len(transcript.interventions),
        ),
    )
