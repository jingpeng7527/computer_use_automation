"""ArtifactStore: where capabilities live on disk.

artifacts/<capability_id>/<version>.json -- one file per version, so a diff
on an artifact is a reviewable change (assignment 3.2's "reviewable" and
"versioned" requirements, using git for both for free -- no database needed
at this scale).
"""

from __future__ import annotations

from pathlib import Path

from .capability import Capability


class ArtifactStore:
    def __init__(self, root: Path | str = "artifacts") -> None:
        self.root = Path(root)

    def _path(self, capability_id: str, version: int) -> Path:
        return self.root / capability_id / f"{version}.json"

    def save(self, cap: Capability) -> Path:
        path = self._path(cap.capability_id, cap.version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cap.model_dump_json(indent=2))
        return path

    def load(self, capability_id: str, version: int | None = None) -> Capability:
        if version is None:
            version = self.latest_version(capability_id)
        path = self._path(capability_id, version)
        return Capability.model_validate_json(path.read_text())

    def latest_version(self, capability_id: str) -> int:
        cap_dir = self.root / capability_id
        versions = [int(p.stem) for p in cap_dir.glob("*.json") if p.stem.isdigit()]
        if not versions:
            raise FileNotFoundError(f"no artifacts for {capability_id!r} under {self.root}")
        return max(versions)

    def list(self) -> list[Capability]:
        if not self.root.exists():
            return []
        caps = []
        for cap_dir in sorted(self.root.iterdir()):
            if not cap_dir.is_dir():
                continue
            caps.append(self.load(cap_dir.name, self.latest_version(cap_dir.name)))
        return caps
