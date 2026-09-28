"""Validation for user supplied evidence reference identifiers."""

import re
from uuid import UUID

from rest_framework import serializers

EVIDENCE_REF_PREFIXES = frozenset(
    {"snapshot", "report", "event", "review", "retrieval", "raw", "semantic", "task", "claim", "metric"}
)
_REFERENCE_VALUE = re.compile(r"^[A-Za-z0-9._-]{1,180}$")


def validate_evidence_refs(refs):
    """Reject ambiguous or executable-looking values used as evidence IDs."""
    if refs is None:
        return
    if len(refs) != len(set(refs)):
        raise serializers.ValidationError("证据引用不能重复。")
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip() or len(ref) > 200:
            raise serializers.ValidationError("证据引用必须是非空、长度不超过 200 的标识。")
        try:
            UUID(ref)
            continue
        except (ValueError, AttributeError):
            pass
        prefix, separator, value = ref.partition(":")
        if (
            not separator
            or prefix not in EVIDENCE_REF_PREFIXES
            or not value.strip()
            or not _REFERENCE_VALUE.fullmatch(value.strip())
        ):
            raise serializers.ValidationError("证据引用必须使用允许的类型前缀或 UUID。")
