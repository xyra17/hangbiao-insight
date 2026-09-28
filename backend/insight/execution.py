"""A job's database writes must belong to its current, live execution."""
from contextvars import ContextVar

from django.db import connection

current_execution = ContextVar("insight_execution", default=None)


def check_execution():
    owner = current_execution.get()
    if owner is None:
        return
    from insight.models import Job

    job_id, lease_token, lock_pid, lock_key = owner
    # Caller holds an atomic block. Retain the row lock until its domain write
    # commits, so a newer attempt cannot replace the generation mid-commit.
    job = Job.objects.select_for_update().get(pk=job_id)
    if job.lease_token != lease_token:
        raise RuntimeError("Job lease superseded")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' "
            "AND pid=%s AND classid=%s AND objid=%s AND objsubid=1 AND granted)",
            [lock_pid, lock_key >> 32, lock_key & 0xFFFFFFFF],
        )
        if not cursor.fetchone()[0]:
            raise RuntimeError("Job lock connection lost")
