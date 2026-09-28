import json
import os
import stat
from datetime import timedelta
from pathlib import Path

from django.core.files import File
from django.db import transaction
from django.utils import timezone

from insight.models import DirectorySource, ImportBatch, Job, Outbox

from .service import accept_upload, require_source_access


@transaction.atomic
def ingest_stable_file(directory, candidate, meta_path, fingerprint):
    from .api import UploadCommand

    with os.fdopen(os.open(meta_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "r") as meta:
        info = os.fstat(meta.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > 16384
            or [info.st_size, info.st_mtime_ns] != fingerprint[2:]
        ):
            raise ValueError("范围声明读取时发生变化")
        declaration = json.load(meta)
        after = os.fstat(meta.fileno())
        if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
            raise ValueError("范围声明读取时发生变化")
    with os.fdopen(os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or [info.st_size, info.st_mtime_ns] != fingerprint[:2]:
            raise ValueError("读取时文件仍在变化")
        upload = File(stream, name=candidate.name)
        serializer = UploadCommand(data={**declaration, "file": upload})
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        data.pop("file")
        batch = accept_upload(directory.actor, directory.source, upload, data, "directory-scan")
        after = os.fstat(stream.fileno())
        if [after.st_size, after.st_mtime_ns] != fingerprint[:2]:
            raise ValueError("读取时文件发生变化")
    return batch


def scan_directory(directory_id):
    """A nested transaction rolls back an import if its file changes during reading."""
    with transaction.atomic():
        directory = (
            DirectorySource.objects.select_for_update(of=("self",))
            .select_related("source__shop", "actor")
            .get(pk=directory_id)
        )
        if not directory.enabled or (directory.next_scan_at and directory.next_scan_at > timezone.now()):
            return
        if directory.actor_id is None:
            directory.error, directory.enabled = "目录尚未绑定授权执行身份，请重新登记。", False
            directory.save(update_fields=["error", "enabled"])
            return
        require_source_access(directory.actor, directory.source.shop)
        root = Path(directory.root)
        if not root.is_dir() or root.is_symlink() or root.resolve() != root.absolute():
            directory.error = "授权目录不可用或路径已变化。"
            directory.next_scan_at = timezone.now() + timedelta(seconds=directory.scan_interval_seconds)
            directory.save(update_fields=["error", "next_scan_at"])
            return
        observations, errors, pending = dict(directory.observations), [], False
        candidates = sorted(root.glob(directory.pattern))
        if not candidates:
            errors.append("授权目录未发现匹配报表，请核对来源是否按时生成。")
        for candidate in candidates:
            meta_path = candidate.with_suffix(candidate.suffix + ".json")
            try:
                info = candidate.lstat()
                if candidate.parent != root or not stat.S_ISREG(info.st_mode):
                    errors.append("发现不允许的链接或子目录文件")
                    continue
                meta_info = meta_path.lstat()
                if not stat.S_ISREG(meta_info.st_mode):
                    errors.append(f"{candidate.name} 缺少有效范围声明文件")
                    continue
            except OSError:
                observations.pop(candidate.name, None)
                errors.append(f"{candidate.name} 或范围声明无法读取或已消失，将在下次扫描重试。")
                continue
            fingerprint = [info.st_size, info.st_mtime_ns, meta_info.st_size, meta_info.st_mtime_ns]
            previous = observations.get(candidate.name, {})
            now = timezone.now().timestamp()
            if previous.get("fingerprint") != fingerprint:
                observations[candidate.name] = {"fingerprint": fingerprint, "observed_at": now}
                pending = True
                continue
            if now - previous.get("observed_at", now) < 5:
                pending = True
                continue
            if previous.get("batch_id"):
                batch = ImportBatch.objects.get(pk=previous["batch_id"])
                if batch.status in {"uploaded", "parsing"}:
                    pending = True
                if (
                    directory.auto_publish
                    and batch.status == "preview"
                    and not batch.error_rows
                    and batch.contract.approved_at
                ):
                    job, created = Job.objects.get_or_create(
                        shop=directory.source.shop,
                        actor=directory.actor,
                        kind="publish_import",
                        idempotency_key=f"directory:{batch.pk}",
                        defaults={
                            "payload": {
                                "batch_id": str(batch.pk),
                                "allow_partial": False,
                                "confirm_contract": False,
                            }
                        },
                    )
                    if created:
                        Outbox.objects.create(job=job)
                continue
            if meta_info.st_size > 16384:
                errors.append(f"{candidate.name} 范围声明过大")
                continue
            try:
                batch = ingest_stable_file(directory, candidate, meta_path, fingerprint)
                observations[candidate.name]["batch_id"] = str(batch.pk)
                pending = True
            except Exception as exc:
                errors.append(f"{candidate.name} 未导入（{type(exc).__name__}），请核对声明和文件。")
        directory.observations, directory.error = observations, "；".join(errors)[:500]
        directory.next_scan_at = timezone.now() + timedelta(
            seconds=5 if pending else directory.scan_interval_seconds
        )
        directory.save(update_fields=["observations", "error", "next_scan_at"])
