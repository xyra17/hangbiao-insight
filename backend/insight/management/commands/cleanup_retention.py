import json

from django.core.management.base import BaseCommand

from insight.retention import cleanup_retention


class Command(BaseCommand):
    help = "按全局和来源契约的较短保留期清理原始文件、索引和旧审计。"

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--workspace", dest="workspace_id")

    def handle(self, *args, **options):
        result = cleanup_retention(dry_run=options["dry_run"], workspace_id=options.get("workspace_id"))
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
