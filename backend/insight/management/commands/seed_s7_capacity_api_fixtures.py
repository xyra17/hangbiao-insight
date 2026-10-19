import json
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from insight.models import (
    ActionTask,
    BusinessEvent,
    EventEvidence,
    InvestigationRun,
    InvestigationStep,
    Membership,
    MetricSnapshot,
    Shop,
    ShopGrant,
)
from insight.semantic.report import compose_report

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


class Command(BaseCommand):
    help = "Seed nonempty, explicitly synthetic S7 business API samples in an isolated capacity workspace."

    def add_arguments(self, parser):
        parser.add_argument("--shop-a", required=True)
        parser.add_argument("--shop-b", required=True)
        parser.add_argument("--smoke-report", type=Path, required=True)
        parser.add_argument("--credentials-file", type=Path, required=True)
        parser.add_argument("--output", type=Path, required=True)

    @transaction.atomic
    def handle(self, *args, **options):
        shops = {
            "shop_a": Shop.objects.select_related("workspace").get(pk=options["shop_a"]),
            "shop_b": Shop.objects.select_related("workspace").get(pk=options["shop_b"]),
        }
        workspace_ids = {shop.workspace_id for shop in shops.values()}
        if len(workspace_ids) != 1 or not all(shop.workspace.is_test for shop in shops.values()):
            raise CommandError("Capacity API fixtures require two shops in the same isolated is_test workspace.")
        if not all(shop.name.startswith("S7 s7-capacity-v1.1 ") for shop in shops.values()):
            raise CommandError("Refusing to seed outside the versioned S7 capacity shops.")

        credentials_path = self._private_path(options["credentials_file"])
        smoke_path = options["smoke_report"].resolve()
        try:
            credential_payload = json.loads(credentials_path.read_text(encoding="utf-8"))
            smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"Cannot read the private account file or smoke report: {type(exc).__name__}.") from exc
        accounts = credential_payload.get("accounts") if isinstance(credential_payload, dict) else None
        if (
            not isinstance(accounts, list)
            or len(accounts) != 10
            or credential_payload.get("account_class") != "synthetic_isolated_capacity_load"
        ):
            raise CommandError("Provide exactly ten scoped synthetic capacity accounts from the isolated importer.")
        if smoke.get("acceptance_status", {}).get("correctness") != "passed":
            raise CommandError("The smoke report must show correctness=passed before API fixtures are seeded.")

        snapshots = {}
        for slot, shop in shops.items():
            result = next(
                (
                    row
                    for row in smoke.get("completed", [])
                    if row.get("shop") == str(shop.pk) and row.get("correctness_status") == "passed"
                ),
                None,
            )
            if result is None:
                raise CommandError(f"The smoke report has no correct snapshot for {slot}.")
            snapshot = MetricSnapshot.objects.select_related("product", "shop").get(pk=result["snapshot_id"])
            if snapshot.shop_id != shop.pk or snapshot.payload.get("daily", {}).get("status") != "ready":
                raise CommandError(f"The {slot} smoke snapshot is not a ready snapshot from the requested shop.")
            snapshots[slot] = snapshot

        User = get_user_model()
        allowed_shop_ids = sorted(str(shop.pk) for shop in shops.values())
        fixture_users = []
        for record in accounts:
            if not isinstance(record, dict) or not record.get("username") or not record.get("password"):
                raise CommandError("The private capacity account file contains an invalid entry.")
            user = User.objects.filter(username=record["username"], is_active=True).first()
            if (
                user is None
                or not user.check_password(record["password"])
                or record.get("shop_ids") != allowed_shop_ids
            ):
                raise CommandError("A capacity account is inactive or its shop scope differs; no account was changed.")
            membership = Membership.objects.filter(user=user, workspace_id=next(iter(workspace_ids))).first()
            if membership is None or membership.is_admin:
                raise CommandError("Capacity API sample accounts must be non-admin members of the is_test workspace.")
            for shop in shops.values():
                grant = ShopGrant.objects.filter(user=user, shop=shop).first()
                if grant is None or grant.roles != ["operator"] or not grant.all_products:
                    raise CommandError("Capacity API sample account grants differ from the bounded operator scope.")
            fixture_users.append(user)

        created = {"events": 0, "reports": 0, "actions": 0, "investigations": 0, "steps": 0}
        now = timezone.now()
        for slot, shop in shops.items():
            snapshot = snapshots[slot]
            asin = snapshot.child_asin
            event, event_created = BusinessEvent.objects.get_or_create(
                shop=shop,
                object_key=f"asin:{asin}",
                family="sales",
                status__in=["pending", "claimed", "observing", "deferred"],
                defaults={
                    "child_asin": asin,
                    "product": snapshot.product,
                    "title": "S7 合成容量 API 样本（非业务事件）",
                    "status": "pending",
                    "priority": 3,
                    "last_cutoff": snapshot.local_date,
                    "last_seen": now,
                },
            )
            if event_created:
                created["events"] += 1
            EventEvidence.objects.get_or_create(
                event=event,
                snapshot=snapshot,
                metric="ordered_units",
                defaults={"detail": {"fixture": "synthetic_s7_capacity_api_load"}},
            )
            _, report_created = compose_report(
                fixture_users[0],
                shop,
                asin,
                snapshot,
                None,
                snapshot.product_id,
                request_id="seed-s7-capacity-api-fixtures",
            )
            created["reports"] += int(report_created)

            shop_users = fixture_users[0::2] if slot == "shop_a" else fixture_users[1::2]
            for user in shop_users:
                title = f"S7 合成 API 负载样本 {user.username}"
                _, action_created = ActionTask.objects.get_or_create(
                    shop=shop,
                    child_asin=asin,
                    title=title,
                    created_by=user,
                    defaults={
                        "event": event,
                        "product": snapshot.product,
                        "assigned_to": user,
                        "status": "completed",
                        "action_type": "investigation",
                        "question": "合成 API 负载样本；不代表已执行的业务动作。",
                        "completion_criteria": "仅供隔离性能采样。",
                        "evidence_refs": [f"snapshot:{snapshot.pk}"],
                        "observed_metrics": ["ordered_units"],
                        "is_formal": False,
                    },
                )
                created["actions"] += int(action_created)
                query = f"S7 synthetic API load fixture {user.username}"
                run, run_created = InvestigationRun.objects.get_or_create(
                    shop=shop,
                    requested_by=user,
                    query=query,
                    defaults={
                        "child_asin": asin,
                        "product": snapshot.product,
                        "status": "completed",
                        "route": "investigation",
                        "max_steps": 1,
                        "steps_used": 1,
                        "budget_units": 1,
                        "stop_reason": "synthetic_api_load_fixture",
                        "state": {"synthetic_fixture": True},
                        "result": {"summary": "Synthetic API-load fixture; not a model result."},
                        "elapsed_ms": 1,
                    },
                )
                created["investigations"] += int(run_created)
                _, step_created = InvestigationStep.objects.get_or_create(
                    run=run,
                    sequence=1,
                    defaults={
                        "node": "fixture",
                        "status": "completed",
                        "input": {"synthetic_fixture": True},
                        "output": {"snapshot_id": str(snapshot.pk)},
                        "state_snapshot": {"synthetic_fixture": True},
                    },
                )
                created["steps"] += int(step_created)

        output = options["output"].resolve()
        if not output.is_relative_to(REPOSITORY_ROOT / "dataset" / "data" / "08_S7容量评测_v1" / "validation"):
            raise CommandError("Fixture reports must stay in the S7 capacity validation directory.")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "scope": "synthetic fixtures in isolated is_test shops only",
                    "shop_ids": allowed_shop_ids,
                    "snapshot_ids": {slot: str(snapshot.pk) for slot, snapshot in snapshots.items()},
                    "account_count": len(fixture_users),
                    "created": created,
                    "checked_at": now.isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.stdout.write(json.dumps({"status": "passed", "created": created, "report": str(output)}, ensure_ascii=False))

    @staticmethod
    def _private_path(path: Path) -> Path:
        target = (REPOSITORY_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        private_root = (REPOSITORY_ROOT / ".local").resolve()
        if not target.is_relative_to(private_root):
            raise CommandError("Capacity API fixture credentials must stay under repository .local.")
        return target
