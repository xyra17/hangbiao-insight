import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
DEBUG = os.getenv("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "drf_spectacular",
    "insight",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "insight.middleware.RequestIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "insight.middleware.SessionVersionMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("PGDATABASE", "insight"),
        "USER": os.getenv("PGUSER", "insight"),
        "PASSWORD": os.environ["INSIGHT_DB_PASSWORD"],
        "HOST": os.getenv("PGHOST", "127.0.0.1"),
        "PORT": os.getenv("PGPORT", "55432"),
        "CONN_MAX_AGE": 0,
    }
}
TIME_ZONE = "UTC"
USE_TZ = True
LANGUAGE_CODE = "zh-hans"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
CSRF_FAILURE_VIEW = "insight.api_errors.csrf_failure"
CSRF_TRUSTED_ORIGINS = os.getenv("CSRF_TRUSTED_ORIGINS", "http://127.0.0.1:5184,http://localhost:5184").split(
    ","
)
SESSION_COOKIE_AGE = 43200
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "insight.api_errors.exception_handler",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_RATES": {"login": "10/min"},
    "NUM_PROXIES": int(os.getenv("LOGIN_TRUSTED_PROXY_COUNT", "0")),
}
SPECTACULAR_SETTINGS = {
    "TITLE": "航标经营工作台", "VERSION": "2.0.0", "COMPONENT_SPLIT_REQUEST": True,
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
    "ENUM_NAME_OVERRIDES": {"EventGroupOperationEnum": ["merge", "split"]},
}
CELERY_BROKER_URL = os.environ["CELERY_BROKER_URL"]
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_BROKER_TRANSPORT_OPTIONS = {"visibility_timeout": 3600}
CELERY_BEAT_SCHEDULE = {"outbox": {"task": "insight.tasks.dispatch_outbox", "schedule": 10.0}}
CELERY_BEAT_SCHEDULE["directory-scan"] = {"task": "insight.tasks.scan_directories", "schedule": 60.0}
CELERY_BEAT_SCHEDULE["daily-scans"] = {"task": "insight.tasks.dispatch_daily_scans", "schedule": 60.0}
# Keep retention independent from scans: one failed unlink or a temporary DB
# outage must not suppress anomaly detection for every shop.
CELERY_BEAT_SCHEDULE["retention-cleanup"] = {"task": "insight.tasks.cleanup_retention_task", "schedule": 86400.0}
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024
STORAGE_ROOT = Path(os.getenv("INSIGHT_STORAGE_ROOT", str(BASE_DIR / "storage")))
