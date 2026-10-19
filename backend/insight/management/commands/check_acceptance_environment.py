from django.core.management.base import BaseCommand

from ._acceptance import require_isolated_acceptance_environment


class Command(BaseCommand):
    help = "Verify that browser acceptance commands target the isolated local database and storage."

    def handle(self, *args, **options):
        details = require_isolated_acceptance_environment()
        self.stdout.write(
            "Acceptance environment OK: "
            f"database={details['database']}, host={details['database_host']}, "
            f"broker={details['broker_host']}, namespace={details['namespace']}, "
            f"storage={details['storage']}, beat={details['beat_schedule']}"
        )
