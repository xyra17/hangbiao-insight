from datetime import date, datetime, time, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from insight.models import (
    ActionTask,
    BusinessEvent,
    DerivedRefresh,
    EventEvidence,
    ImportBatch,
    Job,
    Membership,
    MetricSnapshot,
    Outbox,
    Product,
    ProductMapping,
    Shop,
    ShopGrant,
    StandardReport,
    Workspace,
)

from ._acceptance import require_isolated_acceptance_environment


class Command(BaseCommand):
    help = "Build deterministic browser fixtures in the isolated acceptance workspace only."

    def add_arguments(self, parser):
        parser.add_argument("--as-of", type=date.fromisoformat, default=None)

    @transaction.atomic
    def handle(self, *args, **options):
        require_isolated_acceptance_environment()
        workspace = Workspace.objects.filter(name="航标验收工作区", is_test=True).first()
        if workspace is None:
            raise CommandError("Run seed_acceptance first to create the isolated test workspace.")
        admin = get_user_model().objects.filter(username="accept_admin", is_active=True).first()
        if admin is None or not Membership.objects.filter(
            workspace=workspace, user=admin, is_admin=True
        ).exists():
            raise CommandError("The isolated acceptance administrator is missing.")

        shop = self._shop(workspace, "验收 D：事件处理闭环")
        as_of = options["as_of"]
        if as_of is None:
            as_of = timezone.now().astimezone(ZoneInfo(shop.timezone)).date()
        now = timezone.make_aware(datetime.combine(as_of, time(12)), ZoneInfo(shop.timezone))
        product = self._product(shop, "BWFLOW2601", "合成事件处理样本")
        operator = get_user_model().objects.filter(username="accept_operator_a", is_active=True).first()
        if operator is None:
            raise CommandError("The isolated acceptance operator is missing.")
        ShopGrant.objects.get_or_create(shop=shop, user=operator, defaults={"roles": ["operator"]})

        fingerprint = sha256(f"acceptance-workflow-fixture-v1:{as_of}".encode()).hexdigest()
        snapshot, _ = MetricSnapshot.objects.update_or_create(
            fingerprint=fingerprint,
            defaults={
                "shop": shop,
                "child_asin": product.child_asin,
                "product": product,
                "actor": admin,
                "rule_version": "acceptance-workflow-fixture-v1",
                "source_manifest": {},
                "payload": {"daily": {"signals": []}, "absolute": {"signals": []}},
                "local_date": as_of,
            },
        )
        sales_event = self._event(shop, product, "sales", as_of, now, "合成流程验收事件")
        EventEvidence.objects.get_or_create(
            event=sales_event,
            snapshot=snapshot,
            metric="ordered_units",
            defaults={"detail": {"fixture": True, "as_of": str(as_of)}},
        )
        self._event(shop, product, "experience", as_of, now, "合成关联退货事件")

        review_shop = Shop.objects.filter(
            workspace=workspace, name="测试店铺 A", marketplace="US"
        ).first()
        review_product = Product.objects.filter(shop=review_shop, child_asin="B000TEST01").first()
        if review_shop is None or review_product is None:
            raise CommandError("The standard acceptance product is missing; run seed_acceptance first.")
        review_snapshot, _ = MetricSnapshot.objects.update_or_create(
            fingerprint=sha256(
                f"acceptance-workflow-fixture-v1:{as_of}:{review_shop.pk}".encode()
            ).hexdigest(),
            defaults={
                "shop": review_shop,
                "child_asin": review_product.child_asin,
                "product": review_product,
                "actor": admin,
                "rule_version": "acceptance-workflow-fixture-v1",
                "source_manifest": {},
                "payload": {
                    "daily": {"signals": []},
                    "absolute": {"signals": []},
                    "mapping_versions": {
                        str(mapping.pk): mapping.version
                        for mapping in ProductMapping.objects.filter(
                            product=review_product,
                            effective_from__lte=as_of,
                        ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
                    },
                },
                "local_date": as_of,
            },
        )
        current_manifest = self._source_manifest(review_shop)
        mapping_versions = {
            str(mapping.pk): mapping.version
            for mapping in ProductMapping.objects.filter(
                product=review_product,
                effective_from__lte=as_of,
            ).filter(Q(effective_to__isnull=True) | Q(effective_to__gt=as_of))
        }
        MetricSnapshot.objects.update_or_create(
            fingerprint=sha256(
                f"acceptance-review-current-no-coverage-v1:{as_of}:{review_shop.pk}".encode()
            ).hexdigest(),
            defaults={
                "shop": review_shop,
                "child_asin": review_product.child_asin,
                "product": review_product,
                "actor": admin,
                "rule_version": "acceptance-review-current-no-coverage-v1",
                "source_manifest": current_manifest,
                "payload": {"mode": "live", "mapping_versions": mapping_versions},
                "local_date": as_of,
            },
        )
        stale_manifest = {
            **current_manifest,
            "acceptance-stale-source": {"kind": "sales_traffic", "revision": 999},
        }
        MetricSnapshot.objects.update_or_create(
            fingerprint=sha256(
                f"acceptance-review-stale-source-v1:{as_of}:{review_shop.pk}".encode()
            ).hexdigest(),
            defaults={
                "shop": review_shop,
                "child_asin": review_product.child_asin,
                "product": review_product,
                "actor": admin,
                "rule_version": "acceptance-stale-source-fixture-v1",
                "source_manifest": stale_manifest,
                "payload": {"mode": "live", "mapping_versions": mapping_versions},
                "local_date": as_of - timedelta(days=1),
            },
        )
        review_event = self._event(
            review_shop, review_product, "sales", as_of, now, "合成流程验收事件"
        )
        EventEvidence.objects.get_or_create(
            event=review_event,
            snapshot=review_snapshot,
            metric="ordered_units",
            defaults={"detail": {"fixture": True, "as_of": str(as_of)}},
        )
        comparison = self._review_comparison(as_of)
        ActionTask.objects.update_or_create(
            shop=review_shop,
            child_asin=review_product.child_asin,
            title="S5 复查展示验收",
            created_by=admin,
            defaults={
                "product": review_product,
                "status": "completed",
                "action_type": "listing",
                "question": "完整成熟队列的退货率与件数／会话转化率是否发生变化？",
                "completion_criteria": "仅记录窗口变化，不宣称因果。",
                "effective_at": now - timedelta(days=60),
                "review_at": now,
                "review_status": "completed",
                "review_result": (
                    "合成复查结果：成熟发货队列完整，退货率与件数／会话转化率仅记录前后变化，不作因果结论。"
                    "已保留两窗日期、成熟分母、来源版本和计算口径，供运营追溯。建议下一轮继续观察变化，"
                    "并结合页面与促销记录核对同期因素。"
                ),
                "observed_metrics": ["returns", "unit_session_percent"],
                "evidence_refs": ["synthetic:action-review-fixture", str(snapshot.pk)],
                "review_comparison": {
                    "returns": comparison,
                    "unit_session_percent": self._conversion_review_comparison(as_of),
                },
            },
        )
        ActionTask.objects.update_or_create(
            shop=review_shop,
            child_asin=review_product.child_asin,
            title="S5 缺报与过期快照拒绝验收",
            created_by=admin,
            defaults={
                "product": review_product,
                "status": "completed",
                "action_type": "listing",
                "question": "验证缺报和过期快照不能形成复查结论。",
                "completion_criteria": "拒绝复查且不写入结论。",
                "effective_at": now - timedelta(days=60),
                "review_at": now,
                "review_status": "scheduled",
                "review_result": "",
                "observed_metrics": ["ordered_units"],
                "evidence_refs": ["synthetic:action-review-boundary-fixture"],
                "review_comparison": {},
            },
        )

        metric_shop = Shop.objects.filter(workspace=workspace, name="验收 C：指标与追溯").first()
        metric_product = Product.objects.filter(shop=metric_shop, child_asin="B000TEST03").first()
        batch = ImportBatch.objects.filter(
            contract__source__shop=metric_shop,
            contract__source__kind="sales_traffic",
            status="published",
        ).order_by("-created_at").first()
        if metric_shop is None or metric_product is None or batch is None:
            raise CommandError("The metric acceptance batch is missing; run seed_metric_acceptance first.")
        publication_key = sha256(
            f"acceptance-browser-refresh-report-v1:{metric_shop.pk}".encode()
        ).hexdigest()
        report, _ = StandardReport.objects.update_or_create(
            shop=metric_shop,
            child_asin=metric_product.child_asin,
            product=metric_product,
            report_version=1,
            defaults={
                "publication_key": publication_key,
                "metric_snapshot": None,
                "semantic_run": None,
                "previous_report": None,
                "input_versions": {"fixture": "browser-refresh-retry-v1"},
                "status": "published",
                "payload": {
                    "summary": "隔离浏览器验收报告；用于核对失败派生刷新可安全重试。",
                    "signals": [],
                    "claims": [],
                    "limitations": ["synthetic_browser_acceptance_fixture"],
                },
            },
        )
        retry_key = f"acceptance-browser-refresh-retry-v1:{report.pk}"
        previous_retry = DerivedRefresh.objects.filter(
            artifact_type="report",
            artifact_id=report.pk,
            job__kind="refresh_derived",
            job__idempotency_key=retry_key,
        ).select_related("batch").first()
        if previous_retry is not None:
            batch = previous_retry.batch
        refresh, _ = DerivedRefresh.objects.update_or_create(
            batch=batch,
            artifact_type="report",
            artifact_id=report.pk,
            defaults={
                "shop": metric_shop,
                "replacement_id": None,
                "status": "failed",
                "detail": "浏览器验收故障样本：上次派生刷新中断。",
            },
        )
        job, _ = Job.objects.update_or_create(
            shop=metric_shop,
            actor=admin,
            kind="refresh_derived",
            idempotency_key=retry_key,
            defaults={
                "status": "failed",
                "attempts": 2,
                "error": "浏览器验收故障样本：上次派生刷新中断。",
                "finished_at": timezone.now(),
                "payload": {
                    "prepared_by": admin.pk,
                    "refresh_id": str(refresh.pk),
                    "child_asin": metric_product.child_asin,
                    "product_id": str(metric_product.pk),
                },
            },
        )
        refresh.job = job
        refresh.save(update_fields=["job"])
        outbox, _ = Outbox.objects.get_or_create(job=job)
        outbox.sent_at = timezone.now()
        outbox.save(update_fields=["sent_at"])
        self.stdout.write(
            f"Browser fixtures ready in is_test workspace; as_of={as_of}; "
            "event workflows, action review and stale-source boundaries, derived-refresh retry."
        )

    @staticmethod
    def _shop(workspace, name):
        return Shop.objects.get_or_create(
            workspace=workspace,
            name=name,
            marketplace="US",
            defaults={"timezone": "America/Los_Angeles", "currency": "USD"},
        )[0]

    @staticmethod
    def _product(shop, asin, name):
        product, _ = Product.objects.get_or_create(
            shop=shop,
            child_asin=asin,
            seller_sku=asin,
            fulfillment="FBA",
        )
        ProductMapping.objects.get_or_create(
            product=product,
            version=1,
            defaults={
                "name": name,
                "effective_from": date(2020, 1, 1),
                "reason": "固定合成浏览器验收样本",
            },
        )
        return product

    @staticmethod
    def _event(shop, product, family, as_of, now, title):
        matching = BusinessEvent.objects.filter(
            shop=shop, object_key=f"asin:{product.child_asin}", family=family
        )
        event = matching.exclude(status="closed").order_by("-created_at").first()
        if event is None:
            event = matching.filter(status="closed").order_by("-created_at").first()
        if event is None:
            event = BusinessEvent.objects.create(
                shop=shop,
                object_key=f"asin:{product.child_asin}",
                child_asin=product.child_asin,
                product=product,
                family=family,
                title=title,
                status="pending",
                priority=2,
                last_cutoff=as_of,
                last_seen=now,
            )
        updates = []
        for name, value in {
            "child_asin": product.child_asin,
            "product": product,
            "title": title,
            "status": "pending",
            "priority": 2,
            "last_cutoff": as_of,
            "last_seen": now,
            "closed_at": None,
            "closure_reason": "",
            "closure_note": "",
            "closure_evidence": [],
            "duplicate_of": None,
        }.items():
            if getattr(event, name) != value:
                setattr(event, name, value)
                updates.append(name)
        if updates:
            event.version += 1
            event.save(update_fields=[*updates, "version"])
        return event

    @staticmethod
    def _source_manifest(shop):
        from insight.metrics.facts import source_manifest

        return source_manifest(shop)

    @staticmethod
    def _review_comparison(as_of):
        effective_date = as_of - timedelta(days=60)
        baseline_start, baseline_end = effective_date - timedelta(days=58), effective_date - timedelta(days=31)
        current_start, current_end = effective_date + timedelta(days=1), effective_date + timedelta(days=28)
        return {
            "baseline": "0.12",
            "current": "0.08",
            "delta": "-0.04",
            "unit": "percent",
            "basis": "mature_cohort_rate",
            "baseline_days": 28,
                "current_days": 28,
            "baseline_total": None,
            "current_total": None,
            "window_start": str(current_start),
            "window_end": str(current_end),
            "snapshot": "synthetic-action-review-fixture",
            "source_manifest": {"synthetic-source": {"kind": "returns", "revision": 1}},
            "snapshot_rule_version": "action-review-fixture-v2",
            "method": "action_review_v2",
            "maturity": {
                "status": "ready",
                "observation_days": 30,
                "immature_orders": 0,
                "unverified_first_shipment_orders": 0,
            },
            "window_evidence": {
                "version": "action_review_v2",
                "effective_date": str(effective_date),
                "observation_days": 30,
                "baseline": {
                    "start": str(baseline_start),
                    "end": str(baseline_end),
                    "days": 28,
                    "numerator": 12,
                    "denominator": 100,
                    "rate": "0.12",
                },
                "current": {
                    "start": str(current_start),
                    "end": str(current_end),
                    "days": 28,
                    "numerator": 8,
                    "denominator": 100,
                    "rate": "0.08",
                },
                "late_order_count": 0,
                "unmatched_records": 0,
                "unclassified_status_records": 0,
                "raw_row_ids": ["synthetic-return-001", "synthetic-return-002"],
            },
        }

    @staticmethod
    def _conversion_review_comparison(as_of):
        effective_date = as_of - timedelta(days=60)
        baseline_start, baseline_end = effective_date - timedelta(days=28), effective_date - timedelta(days=1)
        current_start, current_end = effective_date + timedelta(days=1), effective_date + timedelta(days=7)
        return {
            "baseline": "0.125",
            "current": "0.1025",
            "delta": "-0.0225",
            "unit": "percent",
            "basis": "window_value",
            "baseline_days": 28,
            "current_days": 7,
            "window_start": str(current_start),
            "window_end": str(current_end),
            "snapshot": "synthetic-action-review-fixture",
            "source_manifest": {"synthetic-source": {"kind": "sales_traffic", "revision": 1}},
            "snapshot_rule_version": "action-review-fixture-v2",
            "method": "action_review_v2",
            "maturity": {"status": "ready", "immature_orders": 0, "unverified_first_shipment_orders": 0},
            "window_evidence": {
                "version": "action_review_v2",
                "effective_date": str(effective_date),
                "baseline": {
                    "start": str(baseline_start),
                    "end": str(baseline_end),
                    "days": 28,
                    "unit_session_percent": "0.125",
                    "ratio_status": "ready",
                },
                "current": {
                    "start": str(current_start),
                    "end": str(current_end),
                    "days": 7,
                    "unit_session_percent": "0.1025",
                    "ratio_status": "ready",
                },
                "raw_row_ids": ["synthetic-conversion-001", "synthetic-conversion-002"],
            },
        }
