import csv
import io
import json
import os
import secrets
from datetime import UTC, date
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from insight.ingestion.service import accept_upload, create_source, parse_batch, publish_batch
from insight.models import (
    InvestigationRun,
    InvestigationStep,
    Membership,
    Product,
    ProductMapping,
    Shop,
    ShopGrant,
    Source,
    SupportCase,
)

from ._acceptance import require_isolated_acceptance_environment

REPOSITORY_ROOT = Path(settings.BASE_DIR).resolve().parent
CORPUS_ROOT = REPOSITORY_ROOT / "dataset" / "data" / "07_S4_S6独立评测_v1" / "runtime_corpus_v3"
ACCOUNT_COUNT = 10
RETRIEVAL_ASIN = "D5X2600001"
SHOP_NAMES = ("测试店铺 A", "验收 C：指标与追溯")
SEARCH_SHOP_NAME = "S7 API检索性能样本"
SOURCE_NAME = "Synthetic runtime support search sample"
ACCOUNT_ROLES = ["operator", "data_admin"]
SUPPORT_FIELDS = (
    "child_asin",
    "seller_sku",
    "fulfillment",
    "business_date",
    "record_id",
    "text",
    "status",
)


class Command(BaseCommand):
    help = "Prepare isolated S7 API-load fixtures and ten scoped synthetic acceptance accounts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--credentials-output",
            type=Path,
            required=True,
            help="Private credentials JSON path under the repository .local directory.",
        )

    def handle(self, *args, **options):
        environment = require_isolated_acceptance_environment()
        account_path = self._credential_path(options["credentials_output"])
        workspace = self._acceptance_workspace()
        admin = self._acceptance_admin(workspace)
        shops = [self._existing_shop(workspace, name) for name in SHOP_NAMES]
        search_shop = self._search_shop(workspace)
        self._publish_retrieval_sample(admin, search_shop)
        shops.append(search_shop)
        credentials = self._load_or_create_accounts(account_path, shops)
        self._ensure_scoped_grants(credentials, shops)
        self._ensure_investigation_samples(credentials, self._existing_shop(workspace, "测试店铺 A"))
        self.stdout.write(
            "S7 API-load fixtures ready: "
            f"database={environment['database']}; shops={len(shops)}; "
            f"support_documents={SupportCase.objects.filter(source__shop=search_shop).count()}; "
            f"scoped_accounts={len(credentials)}; investigation_samples={len(credentials)}; "
            f"credentials_file={account_path}"
        )

    @staticmethod
    def _credential_path(path: Path) -> Path:
        target = (REPOSITORY_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
        private_root = (REPOSITORY_ROOT / ".local").resolve()
        if not target.is_relative_to(private_root):
            raise CommandError("Credentials must be stored under the repository .local directory.")
        if target.exists():
            return target
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        return target

    @staticmethod
    def _acceptance_workspace():
        from insight.models import Workspace

        workspace = Workspace.objects.filter(name="航标验收工作区", is_test=True).first()
        if workspace is None:
            raise CommandError("Run seed_acceptance before preparing S7 API-load fixtures.")
        return workspace

    @staticmethod
    def _acceptance_admin(workspace):
        admin = get_user_model().objects.filter(username="accept_admin", is_active=True).first()
        if admin is None or not Membership.objects.filter(workspace=workspace, user=admin, is_admin=True).exists():
            raise CommandError("The isolated acceptance administrator is missing.")
        return admin

    @staticmethod
    def _existing_shop(workspace, name):
        shop = Shop.objects.filter(workspace=workspace, name=name, marketplace="US").first()
        if shop is None:
            raise CommandError(f"Required isolated acceptance shop is missing: {name}")
        return shop

    @staticmethod
    def _search_shop(workspace):
        return Shop.objects.get_or_create(
            workspace=workspace,
            name=SEARCH_SHOP_NAME,
            marketplace="US",
            defaults={"timezone": "America/Los_Angeles", "currency": "USD"},
        )[0]

    def _publish_retrieval_sample(self, admin, shop):
        manifest_path = CORPUS_ROOT / "corpus_manifest.json"
        corpus_path = CORPUS_ROOT / "support_cases.csv"
        if not manifest_path.is_file() or not corpus_path.is_file():
            raise CommandError("The generated runtime support corpus is missing; rebuild it with prepare_retrieval_corpus.py.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("retrieval_scope_asin") != RETRIEVAL_ASIN:
            raise CommandError("The runtime corpus retrieval scope does not match this fixture command.")
        with corpus_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = set(reader.fieldnames or [])
            if set(SUPPORT_FIELDS) != fields or fields.intersection(
                {"topic", "query_id", "qrels", "answerable", "expected_route", "expected_behavior"}
            ):
                raise CommandError("Runtime support rows must contain only the approved searchable fields.")
            rows = [row for row in reader if row["child_asin"] == RETRIEVAL_ASIN]
        expected_count = int(manifest["retrieval_document_count"])
        if len(rows) != expected_count or not rows:
            raise CommandError(f"Expected {expected_count} retrieval rows, found {len(rows)}.")

        product, _ = Product.objects.get_or_create(
            shop=shop,
            child_asin=RETRIEVAL_ASIN,
            seller_sku=f"EVAL-{RETRIEVAL_ASIN}",
            fulfillment="FBA",
        )
        ProductMapping.objects.get_or_create(
            product=product,
            version=1,
            defaults={
                "name": "隔离检索压测合成商品",
                "effective_from": date(2020, 1, 1),
                "reason": "S7 API 检索性能夹具",
            },
        )
        source = Source.objects.filter(shop=shop, name=SOURCE_NAME).first()
        if source is None:
            source = create_source(
                admin,
                shop,
                {
                    "name": SOURCE_NAME,
                    "kind": "support",
                    "is_primary": True,
                    "grain": "product_record",
                    "column_mapping": {field: field for field in SUPPORT_FIELDS},
                    "ignored_columns": [],
                    "coverage_basis": "business_date",
                },
                "seed-s7-api-load-fixtures",
            )
        existing_rows = SupportCase.objects.filter(source=source).count()
        if existing_rows == expected_count:
            return
        if existing_rows:
            raise CommandError(
                f"Search fixture already has {existing_rows} support rows; expected {expected_count}. Refusing to replace it."
            )

        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=SUPPORT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        dates = [date.fromisoformat(row["business_date"]) for row in rows]
        batch = accept_upload(
            admin,
            source,
            SimpleUploadedFile("s7-runtime-support-search.csv", stream.getvalue().encode("utf-8")),
            {
                "coverage_start": min(dates),
                "coverage_end": max(dates),
                "generated_at": timezone.now(),
                "declared_complete": True,
                "declared_asins": [RETRIEVAL_ASIN],
            },
            "seed-s7-api-load-fixtures",
        )
        parse_batch(batch.pk, admin)
        batch.refresh_from_db()
        if batch.status != "preview" or batch.error_rows:
            raise CommandError(f"The synthetic retrieval import did not validate: {batch.status}/{batch.error_rows} errors.")
        publish_batch(
            admin,
            batch.pk,
            allow_partial=False,
            confirm_contract=True,
            request_id="seed-s7-api-load-fixtures",
        )

    def _load_or_create_accounts(self, target, shops):
        shop_ids = [str(shop.pk) for shop in shops]
        if target.exists():
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise CommandError(f"Cannot read the existing credentials file: {type(exc).__name__}.") from exc
            records = payload.get("accounts") if isinstance(payload, dict) else None
            if not isinstance(records, list) or len(records) != ACCOUNT_COUNT:
                raise CommandError("The existing credentials file must contain exactly ten accounts.")
            for record in records:
                user = get_user_model().objects.filter(username=record.get("username"), is_active=True).first()
                if (
                    user is None
                    or not user.check_password(record.get("password", ""))
                    or record.get("shop_ids") != shop_ids
                ):
                    raise CommandError("The existing credentials do not match their accounts or shop scope; no passwords were changed.")
            return records

        User = get_user_model()
        credentials = []
        for _ in range(ACCOUNT_COUNT):
            username = f"s7_load_{secrets.token_hex(6)}"
            password = secrets.token_urlsafe(24)
            user = User.objects.create_user(username=username, password=password)
            credentials.append({"username": user.username, "password": password, "shop_ids": shop_ids})
        payload = {
            "account_class": "synthetic_isolated_acceptance",
            "generated_at": timezone.now().astimezone(UTC).isoformat(timespec="seconds"),
            "accounts": credentials,
        }
        with target.open("x", encoding="utf-8") as handle:
            os.chmod(target, 0o600)
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        return credentials

    @staticmethod
    def _ensure_scoped_grants(credentials, shops):
        User = get_user_model()
        workspace_ids = {shop.workspace_id for shop in shops}
        if len(workspace_ids) != 1:
            raise CommandError("All S7 API-load shops must belong to the same isolated workspace.")
        workspace_id = next(iter(workspace_ids))
        for account in credentials:
            user = User.objects.get(username=account["username"], is_active=True)
            if Membership.objects.filter(user=user).exclude(workspace_id=workspace_id).exists():
                raise CommandError("S7 API-load accounts must not belong to another workspace.")
            membership, _ = Membership.objects.get_or_create(
                user=user,
                workspace_id=workspace_id,
                defaults={"is_admin": False},
            )
            if membership.is_admin:
                raise CommandError("S7 API-load accounts must not be workspace administrators.")
            for shop in shops:
                grant, created = ShopGrant.objects.get_or_create(
                    user=user,
                    shop=shop,
                    defaults={"roles": ACCOUNT_ROLES, "all_products": True},
                )
                if not created and (grant.roles != ACCOUNT_ROLES or not grant.all_products):
                    raise CommandError(
                        "An existing S7 account has a different shop role or product scope; refusing to widen it."
                    )

    @staticmethod
    def _ensure_investigation_samples(credentials, shop):
        product = Product.objects.filter(shop=shop, child_asin="B000TEST01").order_by("pk").first()
        if product is None:
            raise CommandError("The S7 investigation list fixture requires the existing acceptance product B000TEST01.")
        User = get_user_model()
        for index, account in enumerate(credentials, start=1):
            user = User.objects.get(username=account["username"], is_active=True)
            query = f"S7 synthetic API-load fixture {index:02d}"
            run, _ = InvestigationRun.objects.get_or_create(
                shop=shop,
                requested_by=user,
                child_asin=product.child_asin,
                query=query,
                defaults={
                    "product": product,
                    "status": "completed",
                    "route": "investigation",
                    "max_steps": 1,
                    "steps_used": 1,
                    "budget_units": 1,
                    "stop_reason": "synthetic_api_load_fixture",
                    "state": {"synthetic_fixture": True},
                    "result": {
                        "summary": "Synthetic API-load fixture; not an evaluated model output.",
                        "synthetic_fixture": True,
                    },
                    "elapsed_ms": 1,
                },
            )
            InvestigationStep.objects.get_or_create(
                run=run,
                sequence=1,
                defaults={
                    "node": "fixture",
                    "tool_name": "",
                    "status": "completed",
                    "input": {"synthetic_fixture": True},
                    "output": {"synthetic_fixture": True},
                    "state_snapshot": {"synthetic_fixture": True},
                },
            )
