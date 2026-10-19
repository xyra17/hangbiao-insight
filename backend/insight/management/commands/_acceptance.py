import re
from pathlib import Path
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import CommandError


def require_isolated_acceptance_environment():
    database = settings.DATABASES["default"]
    database_name = str(database["NAME"])
    database_host = str(database["HOST"] or "")
    broker_host = urlsplit(settings.CELERY_BROKER_URL).hostname
    repository_root = Path(settings.BASE_DIR).resolve().parent
    expected_storage = (repository_root / ".local" / "acceptance-storage" / database_name).resolve()
    actual_storage = Path(settings.STORAGE_ROOT).resolve()

    if not settings.DEBUG:
        raise CommandError("Browser fixtures require local DEBUG mode.")
    if database_name != "insight_acceptance" and not re.fullmatch(
        r"insight_acceptance_[0-9]{8}(?:_v[1-9][0-9]*)?", database_name
    ):
        raise CommandError(
            "Acceptance seed commands require insight_acceptance or a date-isolated "
            "insight_acceptance_YYYYMMDD database."
        )
    if database_host not in {"localhost", "127.0.0.1", "::1"}:
        raise CommandError("Acceptance seed commands require a loopback PostgreSQL host.")
    if broker_host not in {"localhost", "127.0.0.1", "::1"}:
        raise CommandError("Acceptance seed commands require a loopback Redis broker.")
    if settings.ACCEPTANCE_ENVIRONMENT != database_name or settings.CELERY_NAMESPACE != database_name:
        raise CommandError("Acceptance database and Celery namespace must identify the same environment.")
    if settings.CELERY_TASK_DEFAULT_QUEUE != database_name or settings.CELERY_TASK_ROUTES != {"*": {"queue": database_name}}:
        raise CommandError("All acceptance producers and retries must route to their environment queue.")
    if settings.CELERY_BROKER_TRANSPORT_OPTIONS.get("global_keyprefix") != database_name + ":":
        raise CommandError("Acceptance Redis keys must use their environment prefix.")
    schedule = Path(settings.CELERY_BEAT_SCHEDULE_FILENAME).resolve()
    artifact_root = (repository_root / ".local" / "acceptance-artifacts").resolve()
    if not schedule.is_relative_to(artifact_root) or schedule.name != f"beat-{database_name}.db":
        raise CommandError("Acceptance beat schedule must be environment-bound under local artifacts.")
    if actual_storage != expected_storage:
        raise CommandError("Acceptance storage must be environment-bound under .local/acceptance-storage.")

    return {
        "database": database_name,
        "database_host": "loopback",
        "broker_host": "loopback",
        "storage": "local-acceptance",
        "namespace": database_name,
        "beat_schedule": "local-environment-bound",
    }
