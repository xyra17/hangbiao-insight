"""Tasks registered only by the isolated Celery integration workers."""

from django.db import connection

from config.celery import app


@app.task(bind=True, name="isolation_probe.record", ignore_result=True)
def record(self, marker, retry_once=False):
    if retry_once and self.request.retries == 0:
        raise self.retry(countdown=0.1)
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO isolation_probe VALUES (%s, current_database(), %s, %s)",
            [marker, self.request.delivery_info.get("routing_key"), self.request.retries],
        )
