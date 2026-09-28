import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied

from insight.ingestion.directory import ingest_stable_file, scan_directory
from insight.models import DirectorySource, ImportBatch, Membership
from tests_v2.test_ingestion_boundaries import source_sample

pytestmark = pytest.mark.django_db


def inbox(metric_scope, tmp_path):
    users, _, source, _ = source_sample(metric_scope, "sales_traffic")
    root = tmp_path / "inbox"
    root.mkdir()
    report = root / "daily.csv"
    report.write_bytes(b"child_asin,business_date,ordered_units,sessions\nB000TEST01,2026-09-01,10,100\n")
    meta = report.with_suffix(".csv.json")
    meta.write_text(
        json.dumps(
            {
                "coverage_start": "2026-09-01",
                "coverage_end": "2026-09-01",
                "generated_at": "2026-09-02T00:00:00Z",
                "declared_complete": True,
                "declared_asins": ["B000TEST01"],
            }
        )
    )
    directory = DirectorySource.objects.create(source=source, actor=users["admin"], root=str(root))
    return directory, report, meta


def ready(directory):
    scan_directory(directory.pk)
    directory.refresh_from_db()
    directory.observations["daily.csv"]["observed_at"] -= 10
    directory.next_scan_at = timezone.now() - timedelta(seconds=1)
    directory.save()


def test_file_disappearing_between_enumeration_and_stat_is_reported(metric_scope, tmp_path):
    directory, report, _ = inbox(metric_scope, tmp_path)
    original = Path.lstat

    def missing(path, *args, **kwargs):
        if path == report:
            raise FileNotFoundError("test disappearance")
        return original(path, *args, **kwargs)

    with patch.object(Path, "lstat", missing):
        scan_directory(directory.pk)
    directory.refresh_from_db()
    assert "已消失" in directory.error
    assert not ImportBatch.objects.exists()


def test_directory_read_permission_error_is_visible_and_retryable(metric_scope, tmp_path):
    directory, _, _ = inbox(metric_scope, tmp_path)
    ready(directory)
    with patch("insight.ingestion.directory.ingest_stable_file", side_effect=PermissionError("denied")):
        scan_directory(directory.pk)
    directory.refresh_from_db()
    assert "PermissionError" in directory.error
    assert not ImportBatch.objects.exists()
    directory.next_scan_at = timezone.now() - timedelta(seconds=1)
    directory.save()
    scan_directory(directory.pk)
    assert ImportBatch.objects.count() == 1


def test_revoked_directory_identity_cannot_import_stable_file(metric_scope, tmp_path):
    directory, _, _ = inbox(metric_scope, tmp_path)
    ready(directory)
    Membership.objects.filter(user=directory.actor).update(is_admin=False)
    with pytest.raises(PermissionDenied):
        scan_directory(directory.pk)
    assert not ImportBatch.objects.exists()


def test_manifest_mutation_during_read_rolls_back_import(metric_scope, tmp_path):
    directory, report, meta = inbox(metric_scope, tmp_path)
    info, meta_info = report.stat(), meta.stat()
    fingerprint = [info.st_size, info.st_mtime_ns, meta_info.st_size, meta_info.st_mtime_ns]
    original = json.load

    def changed(handle):
        declaration = original(handle)
        with meta.open("a") as output:
            output.write(" ")
        return declaration

    with patch("insight.ingestion.directory.json.load", side_effect=changed):
        with pytest.raises(ValueError, match="变化"):
            ingest_stable_file(directory, report, meta, fingerprint)
    assert not ImportBatch.objects.exists()
