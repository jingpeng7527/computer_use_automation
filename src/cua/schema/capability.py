"""Capability: the artifact itself.

Three readers share one file (assignment 3.2): a calling agent reads
identity/inputs/outputs/possible_outcomes and should never have to read
detection rules to learn what can come back; a human reviewer reads those
plus each step's risk_level; the replay engine reads everything, including
runtime_matches and steps.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .common import Checkpoint, Condition, parse_ref
from .steps import Click, Read, Select, Step, TypeText

SCHEMA_VERSION = "1.0"

ParamType = Literal["string", "integer", "boolean", "date", "enum", "money"]
Sensitivity = Literal["none", "pii", "sensitive"]


class AppProfile(BaseModel):
    """The one version field in this file that is a fact about the world,
    not about us: which vendor product/build this was recorded against.
    `schema_version` versions this file format; `Capability.version` versions
    the capability itself; only `AppProfile.version` is external.

    `base_url` and `allowed_route_patterns` are optional, additional scope
    a capability can declare for itself -- not a second allowlist, a
    NARROWING of the one policy.yaml already enforces. A reviewer reading
    this file can see exactly which routes a capability is meant to touch,
    without cross-referencing policy.yaml; the executor still enforces
    policy.yaml's own allowlist independently and takes the intersection
    (safety/allowlist.py's check_allowed), so an artifact can restrict its
    own reach but never grant itself anything the global policy wouldn't
    already allow. Both default empty/unset, meaning "no additional
    narrowing" -- an artifact that predates this field behaves exactly as
    before."""

    product: str
    version: str
    base_url: str | None = None
    allowed_route_patterns: list[str] = Field(default_factory=list)


class ParamSpec(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    pattern: str | None = None  # validated before replay starts -> fail fast, never touches a browser
    enum_values: list[str] | None = None
    sensitivity: Sensitivity = "none"


class OutputSpec(BaseModel):
    name: str
    type: ParamType
    description: str
    source_step_id: str  # which Read step produces this
    sensitivity: Sensitivity = "none"  # outbound PII (e.g. a member's name) is tagged same as inbound


class RecoveryAction(BaseModel):
    do: Literal["dismiss_dialog", "retry_step", "reload", "wait"]
    target_role: str | None = None
    target_name: str | None = None


class RuntimeMatch(BaseModel):
    """One recognised non-happy-path state. Business-outcome and recoverable
    entries live in ONE list here (not split across a capability-level
    outcomes list and per-step error rules) so replay has a single ordered
    thing to check, terminal entries first, before it ever asserts a step's
    own checkpoint (see replay/engine.py in a later phase)."""

    id: str
    category: Literal["business_outcome", "recoverable", "hard_failure"]
    terminal: bool  # terminal entries are matched before the step checkpoint
    detect: Condition
    after_step: str | None = None  # step id it can occur after; None = any step

    # required iff category == "business_outcome":
    result_code: str | None = None
    result_outputs: list[str] = Field(default_factory=list)

    # required iff category == "recoverable":
    recovery: RecoveryAction | None = None
    max_retries: int = 2

    @model_validator(mode="after")
    def _category_requires_its_fields(self) -> RuntimeMatch:
        if self.category == "business_outcome" and not self.result_code:
            raise ValueError("a business_outcome match requires result_code")
        if self.category == "recoverable" and self.recovery is None:
            raise ValueError("a recoverable match requires a recovery action")
        return self


class RecoveryBudget(BaseModel):
    per_run: int = 5
    # Structurally, not just conventionally, disallowed: a recovery action
    # never gets its own recovery branch. Hardcoding the literal 1 makes
    # "recovery inside recovery" unrepresentable rather than merely unwise.
    max_depth: Literal[1] = 1


class Provenance(BaseModel):
    """How this was discovered, without inlining the transcript -- decoupled
    from the raw model log but still auditable back to it."""

    discovered_at: datetime
    model: str
    goal: str
    discovery_run_id: str  # -> evidence/<discovery_run_id>/
    transcript_sha256: str
    human_edited: bool = False


class Capability(BaseModel):
    schema_version: str = SCHEMA_VERSION
    capability_id: str  # e.g. "acme_core.lookup_savings_balance"
    version: int = 1
    status: Literal["draft", "approved"] = "draft"
    title: str
    summary: str  # one paragraph -- what a calling agent reads to decide to invoke this

    app_profile: AppProfile
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    # Business-outcome CODES only -- no detection logic, no failure codes
    # (those are system-level and identical across every capability). A
    # calling agent needs to know MEMBER_NOT_FOUND is possible before it
    # invokes this, the way an HTTP client knows 404 is possible without
    # reading server code.
    possible_outcomes: list[str] = Field(default_factory=list)

    steps: list[Step] = Field(min_length=1)
    runtime_matches: list[RuntimeMatch] = Field(default_factory=list)
    recovery_budget: RecoveryBudget = Field(default_factory=RecoveryBudget)
    success_condition: Checkpoint

    provenance: Provenance

    @model_validator(mode="after")
    def _possible_outcomes_match_runtime_matches_exactly(self) -> Capability:
        """Bidirectional: a caller must be able to trust `possible_outcomes`
        as the complete, exact list of business outcomes this capability can
        return -- not just a subset of what's real (the old, one-directional
        check), and not a superset either (a code nothing can produce would
        be a promise the engine can't keep)."""
        producible = {
            m.result_code for m in self.runtime_matches if m.category == "business_outcome"
        }
        declared = set(self.possible_outcomes)
        undeclared = producible - declared
        if undeclared:
            raise ValueError(
                f"runtime_matches can produce {sorted(undeclared)}, "
                f"but possible_outcomes doesn't declare them"
            )
        unproducible = declared - producible
        if unproducible:
            raise ValueError(
                f"possible_outcomes declares {sorted(unproducible)}, "
                f"but no runtime_match produces them"
            )
        return self

    @model_validator(mode="after")
    def _referential_integrity(self) -> Capability:
        """Whole-document consistency: no two steps share an id, every
        input/ctx reference has something to bind to (and, for ctx, an
        earlier producer), every declared output is actually produced by
        the step it claims, and every after_step points at a real step.
        None of this is caught by any single field's own validation."""
        step_ids = [s.id for s in self.steps]
        dupes = sorted({i for i in step_ids if step_ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate step ids: {dupes}")

        param_names = {p.name for p in self.inputs}
        output_names = {o.name for o in self.outputs}
        produced_ctx: set[str] = set()
        output_producer: dict[str, str] = {}

        for step in self.steps:
            action = step.action
            if isinstance(action, (TypeText, Select)):
                namespace, name = parse_ref(action.value_from)
                if namespace == "input" and name not in param_names:
                    raise ValueError(
                        f"step {step.id!r} references undeclared input {name!r}"
                    )
                if namespace == "ctx" and name not in produced_ctx:
                    raise ValueError(
                        f"step {step.id!r} references ctx.{name} before any "
                        f"earlier step produces it"
                    )
            elif isinstance(action, Read):
                if action.into.startswith("ctx."):
                    produced_ctx.add(action.into.removeprefix("ctx."))
                elif action.into not in output_names:
                    raise ValueError(
                        f"step {step.id!r} reads into {action.into!r}, "
                        f"which is not a declared output"
                    )
                else:
                    output_producer[action.into] = step.id

        for output in self.outputs:
            if output.source_step_id not in step_ids:
                raise ValueError(
                    f"output {output.name!r} names source_step_id="
                    f"{output.source_step_id!r}, no such step"
                )
            actual_producer = output_producer.get(output.name)
            if actual_producer != output.source_step_id:
                raise ValueError(
                    f"output {output.name!r} declares source_step_id="
                    f"{output.source_step_id!r}, but it is actually produced "
                    f"by step {actual_producer!r}"
                )

        match_ids = [m.id for m in self.runtime_matches]
        match_dupes = sorted({i for i in match_ids if match_ids.count(i) > 1})
        if match_dupes:
            raise ValueError(f"duplicate runtime_match ids: {match_dupes}")
        steps_by_id = {s.id: s for s in self.steps}
        for match in self.runtime_matches:
            if match.after_step is not None and match.after_step not in step_ids:
                raise ValueError(
                    f"runtime_match {match.id!r} names unknown "
                    f"after_step={match.after_step!r}"
                )
            # EVERY recovery.do value re-performs the step's own action --
            # dismiss_dialog and reload do an extra thing FIRST, but
            # replay/engine.py's _apply_match falls through to the SAME
            # retry call at the bottom regardless of `do`. Safe only when
            # that action can't have already taken effect: a checkpoint
            # failing does not prove a click didn't land (a slow POST looks
            # identical), so retrying Click/TypeText/Select risks firing it
            # twice (e.g. a double-submitted payment) no matter which `do`
            # got it there. This used to check `match.recovery.do ==
            # "retry_step"` specifically -- a real gap, since a hardened
            # artifact declaring dismiss_dialog or reload on a Click step
            # sailed through unchecked while carrying the identical risk.
            #
            # Keyed on the action's own TYPE, not `step.risk_level`: that
            # field is a discovered HINT the artifact author writes down
            # (schema/steps.py's own docstring says so), and this file's own
            # design principle elsewhere (系统设计 P5: 权限不由数据自报) is
            # that safety decisions never trust a self-reported field --
            # replay/engine.py's classify_risk() already re-derives risk
            # independently rather than reading Step.risk_level for exactly
            # this reason. A hand-edited or mis-compiled artifact could set
            # risk_level="SAFE_READ" on a Click step with nothing here to
            # catch it; the action's discriminated `type` is not a hint, it
            # is what actually executes, so it can't be mislabelled the same
            # way. Read/Wait/Navigate stay retry-eligible in principle, but
            # replay/engine.py still independently re-checks classify_risk()
            # at the moment of retry, since a Navigate can resolve to an
            # IRREVERSIBLE destination the schema can't see statically.
            if (
                match.category == "recoverable"
                and match.recovery is not None
                and match.after_step is not None
                and isinstance(steps_by_id[match.after_step].action, (Click, TypeText, Select))
            ):
                raise ValueError(
                    f"runtime_match {match.id!r} declares recovery.do={match.recovery.do!r} on "
                    f"step {match.after_step!r}, whose action is "
                    f"{steps_by_id[match.after_step].action.type!r} -- any recovery on "
                    f"click/type/select risks performing it twice, since a failed "
                    f"checkpoint doesn't prove the action didn't already take effect"
                )
        return self

    @model_validator(mode="after")
    def _goal_does_not_leak_sensitive_param_values(self) -> Capability:
        """Schema-level guard against the concrete leak: a discovery goal
        like "look up member 12345..." persists a raw PII value into the
        artifact forever. This is a best-effort, whitespace-token check
        against each sensitive input's own `pattern` -- not a general PII
        scanner (that's safety/redaction.py, built in a later phase). The
        fix at the call site is to store a templated goal, e.g. "look up
        member {{input.member_id}}...", the same reference-by-name
        discipline used everywhere else in this file.

        Checked against both `provenance.goal` and `summary` -- a compiler
        that templates one and forgets the other has fixed nothing, since
        `summary` is the field a calling agent actually reads."""
        for field_name, text in (("provenance.goal", self.provenance.goal), ("summary", self.summary)):
            tokens = re.findall(r"\S+", text)
            for param in self.inputs:
                if param.sensitivity == "none" or not param.pattern:
                    continue
                if any(re.fullmatch(param.pattern, token) for token in tokens):
                    raise ValueError(
                        f"{field_name} appears to contain a literal value for "
                        f"sensitive input {param.name!r} (matches pattern "
                        f"{param.pattern!r}); store a templated value instead, "
                        f"e.g. using {{{{input.{param.name}}}}}"
                    )
        return self
