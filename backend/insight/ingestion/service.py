import csv
import hashlib
import os
import tempfile
from decimal import InvalidOperation
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from insight.access import is_admin, require_role
from insight.api_errors import Conflict
from insight.models import (
    CoveragePartition,
    ImportBatch,
    Job,
    Outbox,
    Product,
    RawFile,
    Shop,
    Source,
    SourceContract,
    StagedRow,
)
from insight.services import audit

from .contracts import FACT_MODELS, digest, load_identity_map, normalize_row, validate_contract

MAX_FILE_SIZE = 50 * 1024 * 1024
CHUNK_SIZE = 1000


def require_source_access(actor, shop):
    require_role(actor, shop, "data_admin")
    if not is_admin(actor, shop) and not shop.shopgrant_set.get(user=actor).all_products:
        raise PermissionDenied("整店报表管理需要该店铺全部商品的数据管理权限。")


@transaction.atomic
def create_source(actor, shop, data, request_id):
    Shop.objects.select_for_update().get(pk=shop.pk)
    require_source_access(actor, shop)
    validate_contract(data["kind"], data["grain"], data["column_mapping"], data["ignored_columns"])
    validate_coverage_contract(data["kind"], data)
    if data["is_primary"] and Source.objects.filter(shop=shop, kind=data["kind"], is_primary=True).exists():
        raise Conflict("该数据域已有主来源，请先核对来源优先级，不叠加两份报表。")
    source = Source.objects.create(
        shop=shop, name=data["name"], kind=data["kind"], is_primary=data["is_primary"]
    )
    SourceContract.objects.create(
        source=source,
        version=1,
        grain=data["grain"],
        column_mapping=data["column_mapping"],
        ignored_columns=data["ignored_columns"],
        coverage_basis=data.get("coverage_basis", "business_date"),
        return_event=data.get("return_event", "received"),
        empty_report_means_zero=data.get("empty_report_means_zero", False),
    )
    audit(actor, shop, "source.created", source.pk, after={"kind": source.kind}, request_id=request_id)
    return source


@transaction.atomic
def revise_contract(actor, source, data, request_id):
    source = Source.objects.select_for_update().get(pk=source.pk)
    require_source_access(actor, source.shop)
    current = source.contracts.order_by("-version").first()
    if data["expected_version"] != current.version:
        raise Conflict()
    validate_contract(source.kind, data["grain"], data["column_mapping"], data["ignored_columns"])
    validate_coverage_contract(source.kind, data)
    contract = SourceContract.objects.create(
        source=source,
        version=current.version + 1,
        grain=data["grain"],
        column_mapping=data["column_mapping"],
        ignored_columns=data["ignored_columns"],
        coverage_basis=data.get("coverage_basis", "business_date"),
        return_event=data.get("return_event", "received"),
        empty_report_means_zero=data.get("empty_report_means_zero", False),
    )
    audit(
        actor,
        source.shop,
        "source.contract_revised",
        source.pk,
        before={"version": current.version},
        after={"version": contract.version},
        request_id=request_id,
    )
    return source


def validate_coverage_contract(kind, data):
    if data.get("empty_report_means_zero") and kind in {"sales_traffic", "inventory", "ratings"}:
        raise ValidationError("日指标来源必须明确提供零值，不能用空文件代替。")
    if data.get("coverage_basis") == "shipped_date":
        if kind != "orders" or not {"shipped_date", "shipped_at"}.intersection(data["column_mapping"]):
            raise ValidationError("发货日期覆盖只适用于提供发货日期或时间的订单来源。")


def store_file(upload):
    settings.STORAGE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    sha = hashlib.sha256()
    total = 0
    fd, temp = tempfile.mkstemp(dir=settings.STORAGE_ROOT)
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in upload.chunks():
                total += len(chunk)
                if total > MAX_FILE_SIZE:
                    raise ValidationError("文件超过 50 MB，请按覆盖窗口分拆报表。")
                sha.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        key = sha.hexdigest()
        destination = settings.STORAGE_ROOT / key
        # Link publishes an immutable file without overwriting an existing object.
        try:
            os.link(temp, destination)
        except FileExistsError:
            pass
        return RawFile.objects.get_or_create(sha256=key, defaults={"storage_key": key, "size": total})[0]
    finally:
        Path(temp).unlink(missing_ok=True)


def accept_upload(actor, source, upload, data, request_id):
    require_source_access(actor, source.shop)
    if not upload.name.lower().endswith(".csv"):
        raise ValidationError("当前接收 CSV 文件。")
    if (
        data["coverage_end"] < data["coverage_start"]
        or (data["coverage_end"] - data["coverage_start"]).days > 3660
    ):
        raise ValidationError("报表日期范围无效或超过十年。")
    asins = sorted(set(data["declared_asins"]))
    if (
        not asins
        or len(asins) > 1000
        or set(asins) - set(Product.objects.filter(shop=source.shop).values_list("child_asin", flat=True))
    ):
        raise ValidationError("报表范围须选择该店铺已映射的 ASIN，最多 1,000 个。")
    raw_file = store_file(upload)
    with transaction.atomic():
        source = Source.objects.select_for_update().get(pk=source.pk)
        require_source_access(actor, source.shop)
        contract = source.contracts.order_by("-version").first()
        manifest = {
            str(p.pk): p.version for p in Product.objects.filter(shop=source.shop, child_asin__in=asins)
        }
        key = digest(
            {
                "contract": str(contract.pk),
                "file": raw_file.sha256,
                "coverage": data,
                "asins": asins,
                "mappings": manifest,
            }
        )
        batch, created = ImportBatch.objects.get_or_create(
            processing_key=key,
            defaults={
                "contract": contract,
                "raw_file": raw_file,
                "actor": actor,
                "filename": Path(upload.name).name[:255],
                **data,
                "declared_asins": asins,
                "mapping_manifest": manifest,
            },
        )
        if created:
            job = Job.objects.create(
                shop=source.shop,
                actor=actor,
                kind="parse_import",
                idempotency_key=str(batch.pk),
                payload={"batch_id": str(batch.pk)},
            )
            Outbox.objects.create(job=job)
            audit(
                actor,
                source.shop,
                "import.accepted",
                batch.pk,
                after={"source_id": str(source.pk), "file_hash": raw_file.sha256},
                request_id=request_id,
            )
        return batch


def parse_batch(batch_id, actor, heartbeat=lambda: None):
    batch = ImportBatch.objects.select_related("contract__source__shop", "raw_file").get(pk=batch_id)
    require_source_access(actor, batch.contract.source.shop)
    if batch.status in {"preview", "invalid", "published"}:
        return
    batch.status = "parsing"
    batch.save(update_fields=["status"])
    contract = batch.contract
    identities = load_identity_map(contract, batch)
    csv.field_size_limit(512 * 1024)
    try:
        with (settings.STORAGE_ROOT / batch.raw_file.storage_key).open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            reader = csv.DictReader(handle)
            headers = reader.fieldnames or []
            mapped = set(contract.column_mapping.values())
            if (
                not headers
                or len(headers) > 128
                or len(set(headers)) != len(headers)
                or mapped - set(headers)
                or set(headers) - mapped - set(contract.ignored_columns)
            ):
                raise ValueError("文件列与已登记契约不一致，请修订映射并重新上传；未知列不会静默丢弃。")
            buffer = []
            for raw in reader:
                line = reader.line_num
                if line <= batch.parse_cursor:
                    continue
                normalized, key, payload_hash, error = {}, "", "", ""
                try:
                    if None in raw or any(value is None for value in raw.values()):
                        raise ValueError("该行列数与表头不一致")
                    normalized, key, payload_hash = normalize_row(contract, batch, raw, identities)
                except (ValueError, InvalidOperation) as exc:
                    error = str(exc)[:500]
                buffer.append(
                    StagedRow(
                        batch=batch,
                        line=line,
                        raw=raw,
                        normalized=normalized,
                        business_key=key,
                        payload_hash=payload_hash,
                        error=error,
                    )
                )
                if len(buffer) >= CHUNK_SIZE:
                    save_chunk(batch, actor, buffer, heartbeat)
                    buffer = []
            if buffer:
                save_chunk(batch, actor, buffer, heartbeat)
        with transaction.atomic():
            from insight.execution import check_execution

            check_execution()
            require_source_access(actor, contract.source.shop)
            # Duplicate keys inside one file are ambiguous, even if their values happen to match.
            duplicates = (
                batch.rows.exclude(business_key="")
                .values("business_key")
                .annotate(n=Count("id"))
                .filter(n__gt=1)
            )
            for duplicate in duplicates:
                batch.rows.filter(business_key=duplicate["business_key"]).update(
                    error="同一文件内业务键重复，请核对来源记录"
                )
            batch.total_rows = batch.rows.count()
            batch.error_rows = batch.rows.exclude(error="").count()
            batch.valid_rows = batch.total_rows - batch.error_rows
            batch.status = "preview" if batch.valid_rows else "invalid"
            if not batch.total_rows:
                if contract.empty_report_means_zero and batch.declared_complete:
                    batch.status = "preview"
                else:
                    batch.error = "空报表不自动证明覆盖完整；请提供有效记录，或明确确认来源契约允许完整空报表代表零事件。"
            batch.save()
    except (UnicodeError, csv.Error, ValueError) as exc:
        with transaction.atomic():
            from insight.execution import check_execution

            check_execution()
            require_source_access(actor, contract.source.shop)
            batch.status, batch.error = "invalid", str(exc)[:500]
            batch.save(update_fields=["status", "error"])


def save_chunk(batch, actor, rows, heartbeat):
    heartbeat()
    require_source_access(actor, batch.contract.source.shop)
    with transaction.atomic():
        from insight.execution import check_execution

        check_execution()
        StagedRow.objects.bulk_create(rows)
        batch.parse_cursor = rows[-1].line
        batch.save(update_fields=["parse_cursor"])


@transaction.atomic
def publish_batch(actor, batch_id, *, allow_partial, confirm_contract, request_id):
    batch = ImportBatch.objects.select_related("contract__source__shop").get(pk=batch_id)
    Shop.objects.select_for_update().get(pk=batch.contract.source.shop_id)
    source = Source.objects.select_for_update().get(pk=batch.contract.source_id)
    require_source_access(actor, source.shop)
    batch.refresh_from_db()
    if batch.status == "published":
        return batch
    if batch.status != "preview":
        raise Conflict("当前批次尚不能发布，请先查看解析结果。")
    contract = SourceContract.objects.select_for_update().get(pk=batch.contract_id)
    if source.contracts.order_by("-version").first().pk != contract.pk:
        raise Conflict("来源契约已更新，请按新契约重新导入。")
    if not contract.approved_at:
        if not confirm_contract:
            raise ValidationError("首次发布须确认字段映射及来源粒度。")
        contract.approved_at, contract.approved_by = timezone.now(), actor
        contract.save(update_fields=["approved_at", "approved_by"])
    if batch.error_rows and not allow_partial:
        raise ValidationError("存在错误行，请修正后重传，或明确按部分数据发布。")
    if batch.total_rows == 0:
        date_field = contract.coverage_basis
        if (
            FACT_MODELS[source.kind]
            .objects.filter(
                source=source,
                child_asin__in=batch.declared_asins,
                **{f"{date_field}__range": (batch.coverage_start, batch.coverage_end)},
            )
            .exists()
        ):
            raise Conflict(
                "所声明的零事件范围已有历史事实；空报表不能撤销历史记录，请提供明确的更正或撤销记录。"
            )
    source.revision += 1
    source.save(update_fields=["revision"])
    model = FACT_MODELS[source.kind]
    affected_asins = set(batch.declared_asins) | set(
        model.objects.filter(
            source=source,
            business_key__in=batch.rows.filter(error="").values("business_key"),
            revision__lt=source.revision,
        )
        .values_list("child_asin", flat=True)
        .distinct()
    )
    facts = []
    for row in batch.rows.filter(error="").iterator(chunk_size=CHUNK_SIZE):
        facts.append(
            model(
                source=source,
                batch=batch,
                raw_row=row,
                business_key=row.business_key,
                payload_hash=row.payload_hash,
                revision=source.revision,
                **row.normalized,
            )
        )
        if len(facts) >= CHUNK_SIZE:
            model.objects.bulk_create(facts)
            facts = []
    if facts:
        model.objects.bulk_create(facts)
    if source.kind in {"reviews", "support"}:
        from insight.semantic.indexing import index_rows

        index_rows(model.objects.filter(batch=batch))
    complete = batch.declared_complete and not batch.error_rows
    if contract.grain.endswith("day"):
        objects = len(batch.declared_asins) if contract.grain == "asin_day" else len(batch.mapping_manifest)
        expected = objects * ((batch.coverage_end - batch.coverage_start).days + 1)
        # Missing daily rows remain unknown, even when an upload is declared complete.
        complete = complete and batch.valid_rows == expected
    CoveragePartition.objects.create(
        source=source,
        batch=batch,
        revision=source.revision,
        start=batch.coverage_start,
        end=batch.coverage_end,
        status="ready" if complete else "partial",
        child_asins=batch.declared_asins,
    )
    batch.status, batch.published_revision, batch.published_at = "published", source.revision, timezone.now()
    batch.save(update_fields=["status", "published_revision", "published_at"])
    audit(
        actor,
        source.shop,
        "import.published",
        batch.pk,
        after={"revision": source.revision, "partial": not complete},
        request_id=request_id,
    )
    from insight.metrics.scheduling import schedule_after_import

    schedule_after_import(actor, batch, sorted(affected_asins))
    return batch
