"""Reference resolution: turns "{{input.x}}" / "{{ctx.x}}" into the actual
literal value at replay time. This is the ONE place a reference becomes a
literal -- every Action upstream of this module carries a reference, never
a value, which is what keeps a raw parameter out of the artifact itself.
"""

from __future__ import annotations

from cua.schema import parse_ref


def resolve_ref(template: str, inputs: dict[str, str], ctx: dict[str, str]) -> str:
    namespace, name = parse_ref(template)
    source = inputs if namespace == "input" else ctx
    if name not in source:
        raise KeyError(f"{template!r} references {namespace}.{name}, which was never provided")
    return source[name]
