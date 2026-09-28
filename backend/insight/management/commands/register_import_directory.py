from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from insight.ingestion.service import require_source_access
from insight.models import DirectorySource, Source
from insight.services import audit


class Command(BaseCommand):
    help = "服务端登记一个经授权的报表目录；Web API 不接收服务器路径。"

    def add_arguments(self, parser):
        parser.add_argument("--source", required=True)
        parser.add_argument("--root", required=True)
        parser.add_argument("--username", required=True)
        parser.add_argument("--pattern", default="*.csv")
        parser.add_argument("--auto-publish", action="store_true")
        parser.add_argument("--interval-seconds", type=int, default=86400)

    @transaction.atomic
    def handle(self, *args, **options):
        source = Source.objects.get(pk=options["source"])
        actor = get_user_model().objects.get(username=options["username"], is_active=True)
        require_source_access(actor, source.shop)
        root = Path(options["root"]).expanduser().absolute()
        if not root.is_dir() or root.resolve() != root or root.is_symlink():
            raise CommandError("目录须存在，且不能包含符号链接。")
        pattern = options["pattern"]
        if "/" in pattern or "\\" in pattern or ".." in pattern or not pattern.endswith(".csv"):
            raise CommandError("模式只允许当前目录中的 CSV 文件。")
        if options["interval_seconds"] < 60:
            raise CommandError("扫描间隔至少 60 秒。")
        directory, _ = DirectorySource.objects.update_or_create(
            source=source,
            defaults={
                "root": str(root),
                "pattern": pattern,
                "actor": actor,
                "auto_publish": options["auto_publish"],
                "observations": {},
                "enabled": True,
                "scan_interval_seconds": options["interval_seconds"],
                "next_scan_at": None,
            },
        )
        audit(
            actor,
            source.shop,
            "source.directory_registered",
            directory.pk,
            after={
                "source_id": str(source.pk),
                "pattern": pattern,
                "auto_publish": directory.auto_publish,
                "scan_interval_seconds": directory.scan_interval_seconds,
            },
        )
        self.stdout.write("目录已登记；文件需提供同名 .csv.json 范围声明，首次契约仍需人工确认。")
