#!/usr/bin/env python3
"""Create an isolated persistent local workspace and import the S7 fixture through the API."""

from __future__ import annotations

import argparse
import csv
import json
import os
import secrets
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parent
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
DEFAULT_PACK = ROOT / "data" / "08_S7容量评测_v1"
DEFAULT_OUTPUT = DEFAULT_PACK / "validation" / "capacity_persistent_import_latest.json"
JOB_TIMEOUT_SECONDS = 1800


def local_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("API URL must be an absolute http(s) URL.")
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("This importer accepts loopback API URLs only.")
    return value.rstrip("/")


def django_setup():
    sys.path.insert(0, str(BACKEND_ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def credential_output_path(value: Path | None = None) -> Path:
    """Resolve private acceptance credentials without relying on a fixed .local file."""
    target = value or Path("backend/.local/s7_capacity_acceptance_credentials.json")
    resolved = target.resolve() if target.is_absolute() else (REPOSITORY_ROOT / target).resolve()
    private_roots = ((REPOSITORY_ROOT / ".local").resolve(), (BACKEND_ROOT / ".local").resolve())
    if not any(resolved.is_relative_to(root) for root in private_roots):
        raise ValueError("Acceptance credentials must be stored under repository .local or backend/.local.")
    return resolved


def create_scoped_load_accounts(shops: dict[str, object], output_path: Path, count: int = 10) -> list[dict[str, object]]:
    """Create non-admin synthetic users scoped only to this is_test capacity workspace."""
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from insight.models import Membership, ShopGrant, Workspace

    if not 1 <= count <= 10:
        raise ValueError("Synthetic capacity load account count must be between 1 and 10.")
    workspace_ids = {shop.workspace_id for shop in shops.values()}
    if len(workspace_ids) != 1:
        raise ValueError("Capacity load accounts must be limited to one isolated workspace.")
    workspace_id = next(iter(workspace_ids))
    workspace = Workspace.objects.get(pk=workspace_id)
    if not workspace.is_test:
        raise ValueError("Synthetic capacity load accounts require an is_test workspace.")
    target = credential_output_path(output_path)
    shop_ids = sorted(str(shop.pk) for shop in shops.values())
    if target.exists():
        payload = json.loads(target.read_text(encoding="utf-8"))
        records = payload.get("accounts") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("account_class") != "synthetic_isolated_capacity_load"
            or not isinstance(records, list)
            or len(records) != count
        ):
            raise ValueError("Existing capacity load credentials do not match the requested isolated account class/count.")
        User = get_user_model()
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("Existing capacity credentials contain an invalid account entry.")
            user = User.objects.filter(username=record.get("username"), is_active=True).first()
            if (
                user is None
                or not user.check_password(record.get("password", ""))
                or record.get("shop_ids") != shop_ids
            ):
                raise ValueError("Existing capacity credentials do not match active scoped accounts; no passwords were changed.")
        return records

    User = get_user_model()
    records: list[dict[str, object]] = []
    for _ in range(count):
        username = f"s7_capacity_load_{secrets.token_hex(6)}"
        password = secrets.token_urlsafe(24)
        user = User.objects.create_user(username=username, password=password)
        Membership.objects.create(workspace=workspace, user=user, is_admin=False)
        for shop in shops.values():
            ShopGrant.objects.create(user=user, shop=shop, roles=["operator"], all_products=True)
        records.append({"username": username, "password": password, "shop_ids": shop_ids})
    payload = {
        "account_class": "synthetic_isolated_capacity_load",
        "generated_at": timezone.now().isoformat(),
        "accounts": records,
    }
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    with target.open("x", encoding="utf-8") as handle:
        os.chmod(target, 0o600)
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return records


def create_persistent_fixture(
    username: str,
    manifest: dict,
    pack_dir: Path,
    *,
    password: str | None = None,
    create_user: bool = False,
) -> tuple[dict[str, object], list[dict[str, str]]]:
    from django.contrib.auth import get_user_model

    from insight.models import Membership, Product, ProductMapping, Shop, Workspace

    package_version = manifest["package_version"]
    user = get_user_model().objects.filter(username=username, is_active=True).first()
    if user is None:
        if not create_user or not password:
            raise RuntimeError("The configured API user is not present in the local Django database.")
        user = get_user_model().objects.create_user(username=username, password=password)
    workspace, _ = Workspace.objects.get_or_create(
        name=f"{package_version} local capacity acceptance",
        defaults={"is_test": True, "timezone": manifest["timezone"]},
    )
    Membership.objects.get_or_create(workspace=workspace, user=user, defaults={"is_admin": True})
    Membership.objects.filter(workspace=workspace, user=user).update(is_admin=True)

    shops: dict[str, object] = {}
    shop_ids: list[dict[str, str]] = []
    for slot in ("shop_a", "shop_b"):
        shop, _ = Shop.objects.get_or_create(
            workspace=workspace,
            name=f"S7 {package_version} {slot}",
            marketplace="US",
            defaults={"timezone": manifest["timezone"], "currency": "USD"},
        )
        shops[slot] = shop
        shop_ids.append({"slot": slot, "shop_id": str(shop.pk)})
        sales_path = pack_dir / "import_ready" / slot / "sales_traffic_v1.csv"
        with sales_path.open(encoding="utf-8-sig", newline="") as handle:
            asins = sorted({row["child_asin"] for row in csv.DictReader(handle)})
        products = []
        for asin in asins:
            product, _ = Product.objects.get_or_create(
                shop=shop,
                child_asin=asin,
                seller_sku=f"{asin}-CAP",
                fulfillment="FBA",
            )
            products.append(product)
        existing_mapping_ids = set(
            ProductMapping.objects.filter(product__in=products, version=1).values_list("product_id", flat=True)
        )
        ProductMapping.objects.bulk_create(
            [
                ProductMapping(
                    product=product,
                    name=f"Synthetic capacity fixture {product.child_asin}",
                    effective_from=date(2020, 1, 1),
                    version=1,
                    reason=f"{package_version} persistent local acceptance fixture",
                )
                for product in products
                if product.pk not in existing_mapping_ids
            ],
            batch_size=500,
            ignore_conflicts=True,
        )
    return shops, shop_ids


def suspend_auto_scans(shops: dict[str, object], username: str) -> int:
    from django.utils import timezone

    from insight.models import DailyScanSchedule, Job, Source

    shop_ids = [shop.pk for shop in shops.values()]
    Source.objects.filter(shop_id__in=shop_ids, is_primary=True).update(is_primary=False)
    DailyScanSchedule.objects.filter(shop_id__in=shop_ids).update(enabled=False)
    return Job.objects.filter(
        shop_id__in=shop_ids,
        actor__username=username,
        kind="scan_metrics",
        status__in=["queued", "retry_wait"],
    ).update(status="cancelled", finished_at=timezone.now(), error="Cancelled before the isolated S7 capacity run.")


def activate_primary_sources(shops: dict[str, object]) -> int:
    from insight.models import Source

    promoted = 0
    for shop in shops.values():
        promoted += Source.objects.filter(
            shop=shop,
            kind__in=["sales_traffic", "orders", "support"],
            is_primary=False,
        ).update(is_primary=True)
    return promoted


def authenticated_client(base_url: str, username: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=base_url, timeout=600)
    response = client.get("/api/v1/session/")
    response.raise_for_status()
    login = client.post(
        "/api/v1/login/",
        json={"username": username, "password": password},
        headers={"X-CSRFToken": response.json()["csrf_token"]},
    )
    login.raise_for_status()
    client.headers["X-CSRFToken"] = login.json()["csrf_token"]
    session = client.get("/api/v1/session/")
    session.raise_for_status()
    if not session.json().get("authenticated"):
        raise RuntimeError("Local API session did not authenticate after login.")
    return client


@contextmanager
def client_session(base_url: str, username: str, password: str):
    client = authenticated_client(base_url, username, password)
    try:
        yield client
    finally:
        client.close()


def split_order_file(path: Path, sidecar: dict, output_dir: Path, *, window_days: int = 30) -> list[dict]:
    """Shard large orders by disjoint shipped-date windows under the 50 MiB API limit."""
    if window_days < 1:
        raise ValueError("window_days must be positive")
    declaration = sidecar["upload_payload"]
    start = date.fromisoformat(declaration["coverage_start"])
    end = date.fromisoformat(declaration["coverage_end"])
    windows = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=window_days - 1), end)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)

    output_dir.mkdir(parents=True, exist_ok=True)
    files = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        fieldnames = list(reader.fieldnames or [])
        if "shipped_date" not in fieldnames:
            raise ValueError(f"{path} is missing shipped_date.")
        writers = []
        handles = []
        try:
            for index, (window_start, window_end) in enumerate(windows, start=1):
                shard_path = output_dir / f"orders_window_{index:02d}.csv"
                handle = shard_path.open("w", encoding="utf-8", newline="")
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                handles.append(handle)
                writers.append(writer)
                shard_meta = json.loads(json.dumps(sidecar))
                shard_meta["upload_payload"]["coverage_start"] = window_start.isoformat()
                shard_meta["upload_payload"]["coverage_end"] = window_end.isoformat()
                files.append({"path": shard_path, "sidecar": shard_meta, "rows": 0})
            for row in reader:
                shipped = date.fromisoformat(row["shipped_date"])
                index = (shipped - start).days // window_days
                if index >= len(windows) or not (windows[index][0] <= shipped <= windows[index][1]):
                    raise ValueError(f"Order row date {shipped} is outside the declared coverage window.")
                writers[index].writerow(row)
                files[index]["rows"] += 1
        finally:
            for handle in handles:
                handle.close()
    if sum(item["rows"] for item in files) == 0:
        raise ValueError(f"No order rows found in {path}.")
    return files


def poll_job(client: httpx.Client, job_id: str) -> dict:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}/")
        response.raise_for_status()
        job = response.json()
        if job["status"] in {"completed", "failed", "cancelled"}:
            if job["status"] != "completed":
                raise RuntimeError(f"Local import job ended with status {job['status']}.")
            return job
        time.sleep(0.5)
    raise TimeoutError("Timed out waiting for a local import job.")


def upload_and_publish(client: httpx.Client, shop_id: str, path: Path, sources: dict[str, str], sidecar_override: dict | None = None) -> dict:
    from insight.models import Job, Source

    sidecar = sidecar_override or json.loads(Path(str(path) + ".meta.json").read_text(encoding="utf-8"))
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError(f"{path.name} exceeds the ingestion API's 50 MiB upload limit.")
    source_payload = dict(sidecar["source_payload"])
    source_payload["shop"] = shop_id
    # Keep imports staged until the complete fact set is present. Each primary
    # source publish otherwise fans out an automatic scan over the whole shop.
    source_payload["is_primary"] = False
    kind = source_payload["kind"]
    source_id = sources.get(kind)
    if source_id is None:
        existing_source = Source.objects.filter(shop_id=shop_id, kind=kind).order_by("-created_at").first()
        if existing_source:
            source_id = str(existing_source.pk)
        else:
            response = client.post("/api/v1/sources/", json=source_payload)
            response.raise_for_status()
            source_id = response.json()["id"]
        sources[kind] = source_id

    declaration = sidecar["upload_payload"]
    multipart_fields = [
        ("coverage_start", (None, declaration["coverage_start"])),
        ("coverage_end", (None, declaration["coverage_end"])),
        ("generated_at", (None, declaration["generated_at"])),
        ("declared_complete", (None, str(declaration["declared_complete"]).lower())),
        *[("declared_asins", (None, asin)) for asin in declaration["declared_asins"]],
    ]
    with path.open("rb") as handle:
        multipart_fields.insert(0, ("file", (path.name, handle, "text/csv")))
        response = client.post(
            f"/api/v1/sources/{source_id}/upload/",
            files=multipart_fields,
            timeout=600,
        )
    response.raise_for_status()
    batch = response.json()
    batch_id = batch["id"]
    if batch["status"] != "published":
        parse_job = Job.objects.filter(kind="parse_import", idempotency_key=str(batch_id)).first()
        if parse_job is None:
            raise RuntimeError("Upload was accepted without its parse job.")
        poll_job(client, str(parse_job.pk))
        batch_response = client.get(f"/api/v1/imports/{batch_id}/")
        batch_response.raise_for_status()
        batch = batch_response.json()
        if batch["status"] != "preview" or batch["error_rows"]:
            raise RuntimeError(f"Import preview rejected {path.name}.")
        publish_response = client.post(
            f"/api/v1/imports/{batch_id}/publish/",
            json={"allow_partial": False, "confirm_contract": True},
        )
        publish_response.raise_for_status()
        publish_job = poll_job(client, publish_response.json()["id"])
        batch_response = client.get(f"/api/v1/imports/{batch_id}/")
        batch_response.raise_for_status()
        batch = batch_response.json()
        if batch["status"] != "published":
            raise RuntimeError(f"Import publish job {publish_job['id']} did not publish {path.name}.")
    return {
        "filename": path.name,
        "batch_id": batch_id,
        "source_id": source_id,
        "rows": batch["total_rows"],
        "status": batch["status"],
        "coverage_start": declaration["coverage_start"],
        "coverage_end": declaration["coverage_end"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base-url", default=os.getenv("INSIGHT_API_BASE_URL", "http://127.0.0.1:8280"))
    parser.add_argument("--username", default=os.getenv("INSIGHT_API_USERNAME"))
    parser.add_argument("--password", default=os.getenv("INSIGHT_API_PASSWORD"))
    parser.add_argument("--pack-dir", type=Path, default=DEFAULT_PACK)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--credentials-output",
        type=Path,
        help="Private .local JSON path for a newly created isolated acceptance account; defaults to backend/.local/s7_capacity_acceptance_credentials.json.",
    )
    parser.add_argument(
        "--load-accounts-output",
        type=Path,
        help="Optional private JSON path under .local for up to ten scoped synthetic capacity load accounts.",
    )
    parser.add_argument("--load-account-count", type=int, default=10)
    args = parser.parse_args()
    if (not args.username or not args.password) and (args.username or args.password):
        parser.error("Pass both API credential options, or leave both unset for an isolated local acceptance account.")
    try:
        base_url = local_base_url(args.api_base_url)
    except ValueError as exc:
        parser.error(str(exc))

    django_setup()
    from insight.models import OrderLine, SalesTrafficDaily, SupportCase

    manifest = json.loads((args.pack_dir / "manifest.json").read_text(encoding="utf-8"))
    generated_credentials = False
    credential_path = credential_output_path(args.credentials_output)
    if not args.username:
        if credential_path.exists():
            credential_data = json.loads(credential_path.read_text(encoding="utf-8"))
            args.username = credential_data["username"]
            args.password = credential_data["password"]
        else:
            args.username = f"s7_capacity_{manifest['as_of'].replace('-', '')}_v11"
            args.password = secrets.token_urlsafe(36)
            generated_credentials = True
    with httpx.Client(base_url=base_url, timeout=10) as probe:
        health = probe.get("/api/v1/health/")
        health.raise_for_status()
        if health.json().get("status") != "ready":
            raise RuntimeError("The loopback API is not ready for local acceptance import.")
    started = time.perf_counter()
    shops, shop_ids = create_persistent_fixture(
        args.username,
        manifest,
        args.pack_dir,
        password=args.password,
        create_user=generated_credentials,
    )
    load_accounts = []
    if args.load_accounts_output is not None:
        try:
            load_accounts = create_scoped_load_accounts(shops, args.load_accounts_output, args.load_account_count)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            parser.error(str(exc))
    auto_scan_jobs_cancelled = suspend_auto_scans(shops, args.username)
    if generated_credentials:
        credential_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        credential_path.write_text(json.dumps({"username": args.username, "password": args.password}) + "\n", encoding="utf-8")
        credential_path.chmod(0o600)
    imported = []
    with client_session(base_url, args.username, args.password) as client:
        with tempfile.TemporaryDirectory(prefix="s7-capacity-order-shards-") as shard_root:
            for slot, shop in shops.items():
                source_ids = {}
                slot_dir = args.pack_dir / "import_ready" / slot
                for filename in ("sales_traffic_v1.csv", "orders_v1.csv", "support_cases_v1.csv", "sales_traffic_revision_v2.csv"):
                    source_path = slot_dir / filename
                    if filename == "orders_v1.csv":
                        sidecar = json.loads(Path(str(source_path) + ".meta.json").read_text(encoding="utf-8"))
                        order_parts = split_order_file(
                            source_path,
                            sidecar,
                            Path(shard_root) / slot,
                            window_days=30,
                        )
                        for part_index, part in enumerate(order_parts, start=1):
                            result = upload_and_publish(
                                client,
                                str(shop.pk),
                                part["path"],
                                source_ids,
                                part["sidecar"],
                            )
                            result["source_filename"] = filename
                            result["coverage_shard"] = part_index
                            result["prepared_row_count"] = part["rows"]
                            imported.append(result)
                        continue
                    imported.append(upload_and_publish(
                        client,
                        str(shop.pk),
                        source_path,
                        source_ids,
                    ))

        primary_sources_promoted = activate_primary_sources(shops)

    shop_ids_only = [shop_id["shop_id"] for shop_id in shop_ids]
    fact_counts = {
        "sales_traffic_daily_rows": SalesTrafficDaily.objects.filter(source__shop_id__in=shop_ids_only).count(),
        "order_lines": OrderLine.objects.filter(source__shop_id__in=shop_ids_only).count(),
        "support_cases": SupportCase.objects.filter(source__shop_id__in=shop_ids_only).count(),
        "published_import_rows": sum(item["rows"] for item in imported),
    }
    expected_counts = manifest["actual_counts"]
    expected_import_rows = expected_counts["total_fact_rows"]
    expected = {
        "sales_traffic_daily_rows": expected_counts["daily_sales_rows"] + expected_counts["version_2_corrections"],
        "order_lines": expected_counts["order_lines"],
        "support_cases": expected_counts["support_cases"],
        "published_import_rows": expected_import_rows,
    }
    result = {
        "status": "passed" if fact_counts == expected and len(imported) == 18 and all(item["status"] == "published" for item in imported) else "failed",
        "as_of": manifest["as_of"],
        "package_version": manifest["package_version"],
        "workspace": f"{manifest['package_version']} local capacity acceptance",
        "workspace_is_test": True,
        "shop_count": len(shop_ids),
        "synthetic_load_account_count": len(load_accounts),
        "synthetic_load_account_class": "synthetic_isolated_capacity_load" if load_accounts else None,
        "shops": shop_ids,
        "coverage_window": manifest["coverage_window"],
        "imported_batch_count": len(imported),
        "auto_scan_jobs_cancelled": auto_scan_jobs_cancelled,
        "primary_sources_promoted": primary_sources_promoted,
        "import_duration_seconds": round(time.perf_counter() - started, 3),
        "fact_counts": fact_counts,
        "expected_fact_counts": expected,
        "index_build_ms": None,
        "imported_batches": imported,
        "environment": "persistent_local_database_loopback_ingestion_api",
        "target_environment_status": "local_measurement_only_target_acceptance_not_implied",
        "scope_note": "Synthetic S7 data was published into a versioned is_test workspace through the local ingestion API. Index-build time is not independently exposed by the application and remains null.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key not in {"shops", "imported_batches"}}, ensure_ascii=False, indent=2))
    print(f"Result: {args.output}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
