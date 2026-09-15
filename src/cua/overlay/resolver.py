"""apply_overlay(): resolves a base Capability plus a tenant Overlay into a
concrete, replayable Capability -- the mechanism REPORT.md sec 4 argues for
but, until now, only the schema for existed.

Mechanical, not clever: walk `overlay.overrides` in order, mutating a plain
list of steps, then hand the result to Capability.model_validate(). That
last step is deliberate, not an afterthought -- it means every existing
invariant this schema already enforces (no duplicate step ids, every
input/ctx/output reference has a surviving producer, RuntimeMatch.after_step
names a real step, ...) is re-checked on the RESOLVED capability for free,
without this module duplicating any of that logic. An overlay that breaks a
downstream reference (skips a step whose ctx output a later step consumes)
is rejected here, before a browser is ever involved -- exactly the
"reference-integrity check... at load time" REPORT.md promises.
"""

from __future__ import annotations

from cua.schema import (
    Capability,
    InsertAfter,
    Navigate,
    Overlay,
    ReplaceTarget,
    ReplaceValue,
    Skip,
    Step,
)


def apply_overlay(base: Capability, overlay: Overlay) -> Capability:
    expected = f"{base.capability_id}@{base.version}"
    if overlay.extends != expected:
        raise ValueError(
            f"overlay for tenant {overlay.tenant_id!r} extends {overlay.extends!r}, "
            f"but this base capability is {expected!r} -- an overlay must name the "
            f"exact version it was built against, not a range or the latest"
        )

    steps: list[Step] = list(base.steps)

    def _index_of(step_id: str) -> int:
        for i, s in enumerate(steps):
            if s.id == step_id:
                return i
        raise ValueError(
            f"overlay for tenant {overlay.tenant_id!r} references unknown step_id {step_id!r}"
        )

    for op in overlay.overrides:
        if isinstance(op, ReplaceTarget):
            idx = _index_of(op.step_id)
            steps[idx] = steps[idx].model_copy(update={"target": op.target})

        elif isinstance(op, ReplaceValue):
            idx = _index_of(op.step_id)
            action = steps[idx].action
            if isinstance(action, Navigate):
                new_action = action.model_copy(update={"url_template": op.value})
            else:
                # ValueError, not TypeError: this is a tenant-data mismatch
                # (the same class of error every other rejection in this
                # function raises), not a Python type-checking failure.
                raise ValueError(  # noqa: TRY004
                    f"overlay for tenant {overlay.tenant_id!r}: replace_value on step "
                    f"{op.step_id!r} ({action.type!r}) is not supported -- only navigate "
                    f"steps have a single literal value this operation can replace"
                )
            steps[idx] = steps[idx].model_copy(update={"action": new_action})

        elif isinstance(op, Skip):
            idx = _index_of(op.step_id)
            del steps[idx]

        elif isinstance(op, InsertAfter):
            idx = _index_of(op.step_id)
            new_step = Step.model_validate(op.step)
            steps.insert(idx + 1, new_step)

        else:  # pragma: no cover -- discriminated union, exhaustive by construction
            raise TypeError(f"unhandled overlay op: {op!r}")

    existing_input_names = {p.name for p in base.inputs}
    colliding = existing_input_names & {p.name for p in overlay.add_inputs}
    if colliding:
        raise ValueError(
            f"overlay for tenant {overlay.tenant_id!r} declares add_inputs {sorted(colliding)} "
            f"that the base capability already declares -- rename the tenant-specific input "
            f"or drop it from add_inputs if it's really the same one"
        )

    # Round-tripping through model_validate (not model_copy, which skips
    # validation) is the whole point: every cross-field invariant
    # Capability already enforces -- duplicate ids, dangling references,
    # RuntimeMatch.after_step naming a real step -- runs again here, against
    # the RESOLVED step list, not the base's. A tenant delta that breaks one
    # of those is rejected now, not discovered mid-replay.
    base_dict = base.model_dump(mode="json")
    resolved = Capability.model_validate(
        {
            **base_dict,
            "capability_id": f"{base.capability_id}.{overlay.tenant_id}",
            "version": 1,
            "status": "draft",  # a resolved overlay is new composition -- review it, don't inherit approval
            "inputs": [*base_dict["inputs"], *(p.model_dump(mode="json") for p in overlay.add_inputs)],
            "steps": [s.model_dump(mode="json") for s in steps],
            "provenance": {**base_dict["provenance"], "human_edited": True},
        }
    )
    return resolved
