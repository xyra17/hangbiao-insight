from django.core.management.base import BaseCommand

from insight.models import Review, SupportCase
from insight.semantic.indexing import index_rows


class Command(BaseCommand):
    help = "Rebuild review and support search indexes without changing source facts."

    def handle(self, *args, **options):
        for model in (Review, SupportCase):
            count = index_rows(model.objects.all())
            self.stdout.write(f"{model.__name__}: indexed {count} rows")
