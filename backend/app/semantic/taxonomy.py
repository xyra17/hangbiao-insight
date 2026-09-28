"""Frozen taxonomy loading and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Taxonomy:
    version: str
    status: str
    topic_structure: dict[str, tuple[str, ...]]

    @property
    def primary_topics(self) -> tuple[str, ...]:
        return tuple(self.topic_structure)

    @property
    def secondary_topics(self) -> tuple[str, ...]:
        return tuple(topic for topics in self.topic_structure.values() for topic in topics)

    def is_valid(self, primary_topic: str, secondary_topic: str) -> bool:
        return secondary_topic in self.topic_structure.get(primary_topic, ())

    def primary_for(self, secondary_topic: str) -> str | None:
        return next(
            (primary for primary, children in self.topic_structure.items() if secondary_topic in children),
            None,
        )


class TaxonomyLoader:
    EXPECTED_VERSION = "taxonomy-v1.1"

    @classmethod
    def load(cls, path: str | Path, *, require_frozen: bool = True) -> Taxonomy:
        source = Path(path)
        data = json.loads(source.read_text(encoding="utf-8"))
        version = data.get("version")
        status = data.get("status", "")
        structure = data.get("topic_structure")
        if version != cls.EXPECTED_VERSION:
            raise ValueError(f"Expected {cls.EXPECTED_VERSION}, got {version!r}")
        if require_frozen and status != "frozen":
            raise ValueError(f"Taxonomy must be frozen, got {status!r}")
        if not isinstance(structure, dict) or not structure:
            raise ValueError("topic_structure must be a non-empty object")

        normalized: dict[str, tuple[str, ...]] = {}
        seen_secondary: set[str] = set()
        for primary, children in structure.items():
            if not isinstance(primary, str) or not isinstance(children, list) or not children:
                raise ValueError("Every primary topic must have a non-empty topic list")
            child_tuple = tuple(children)
            duplicates = seen_secondary.intersection(child_tuple)
            if duplicates:
                raise ValueError(f"Secondary topics must have one parent: {sorted(duplicates)}")
            seen_secondary.update(child_tuple)
            normalized[primary] = child_tuple
        return Taxonomy(version=version, status=status, topic_structure=normalized)
