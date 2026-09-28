import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from django.conf import settings
from django.db import connection

from insight.ingestion.service import parse_batch
from tests_v2.test_import_recovery import upload
from tests_v2.test_ingestion_boundaries import source_sample

pytestmark = pytest.mark.django_db(transaction=True)


def test_real_parser_process_killed_after_chunk_resumes_without_duplicates(metric_scope, tmp_path):
    users, _, source, _ = source_sample(metric_scope, "reviews")
    content = "child_asin,business_date,record_id,title,text,rating,language,collected_at,is_deleted\n" + "".join(f"B000TEST01,2026-09-01,R{i},Title,Review {i},3,en,2026-09-02T00:00:00Z,false\n" for i in range(5))
    batch = upload(users["admin"], source, content.encode())
    marker = tmp_path / "committed"
    code = '''
import sys, time, django
from pathlib import Path
django.setup()
from django.contrib.auth import get_user_model
from insight.ingestion import service
service.CHUNK_SIZE = 2
save = service.save_chunk
def pause(*args, **kwargs):
    save(*args, **kwargs)
    Path(sys.argv[3]).write_text("committed")
    time.sleep(30)
service.save_chunk = pause
service.parse_batch(sys.argv[1], get_user_model().objects.get(pk=sys.argv[2]))
'''
    db = connection.settings_dict
    env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings", "PGDATABASE": db["NAME"], "INSIGHT_STORAGE_ROOT": str(settings.STORAGE_ROOT)}
    process = subprocess.Popen([sys.executable, "-c", code, str(batch.pk), str(users["admin"].pk), str(marker)], env=env, cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "parser did not commit its first chunk"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)
    batch.refresh_from_db()
    assert batch.rows.count() == 2 and batch.parse_cursor == 3
    with patch("insight.ingestion.service.CHUNK_SIZE", 2):
        parse_batch(batch.pk, users["admin"])
    batch.refresh_from_db()
    assert batch.status == "preview" and batch.valid_rows == 5 and batch.rows.count() == 5
