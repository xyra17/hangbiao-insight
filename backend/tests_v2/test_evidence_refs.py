import pytest
from rest_framework.exceptions import ValidationError

from insight.evidence_refs import validate_evidence_refs

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    "refs",
    [
        ["snapshot:abc"],
        ["report:2026-09-28.v1", "retrieval:abc_123"],
        ["00000000-0000-0000-0000-000000000000"],
    ],
)
def test_evidence_reference_types_are_explicit_and_stable(refs):
    validate_evidence_refs(refs)


@pytest.mark.parametrize(
    "refs",
    [
        [],
        ["snapshot:one", "snapshot:one"],
        ["https://example.test/evidence"],
        ["file:/tmp/evidence.csv"],
        ["snapshot:free text"],
        ["unknown:abc"],
        ["raw:row/with/path"],
    ],
)
def test_evidence_reference_rejects_duplicates_paths_urls_and_unknown_types(refs):
    if refs == []:
        validate_evidence_refs(refs)
        return
    with pytest.raises(ValidationError):
        validate_evidence_refs(refs)
