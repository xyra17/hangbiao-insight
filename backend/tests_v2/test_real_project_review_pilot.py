"""One frozen, API-to-worker project investigation per blind pilot dispatch.

The module is inert unless PROJECT_REVIEW_MODE is explicitly set to ``offline``
or ``paid``. Offline mode substitutes only the provider transport; API creation,
the project Job worker, graph, policy, tools, persistence, and output composer run.
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import os
import sys
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from prepare_project_review_pilot import (  # noqa: E402
    ARTIFACTS,
    FROZEN,
    LEDGER,
    MAX_MODEL_ACTIONS,
    MAX_MODEL_REQUESTS_PER_RUN,
    MAX_OUTPUT_TOKENS,
    MAX_REQUEST_BYTES,
    read_json,
    read_jsonl,
)

MODE = os.getenv("PROJECT_REVIEW_MODE", "").strip().lower()
PHASE = os.getenv("PROJECT_REVIEW_PHASE", "smoke").strip().lower()
_PAID_BALANCE_RECEIPT = None
USD_HARD_CAP = Decimal("10.00")
USD_PER_REQUEST = Decimal("0.0112704")
CNY_PER_REQUEST = Decimal("0.075136")
MAX_STEPS = 6
LOCAL_OUTPUT_FAILURE_CODES = {
    "planner_contract_invalid",
    "duplicate_text_evidence_request",
    "structured_output_json_invalid",
    "structured_output_schema_invalid",
    "structured_output_fence_invalid",
    "output_truncated",
    "empty_output_text",
}

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(MODE not in {"offline", "paid"}, reason="explicit project review pilot mode required"),
]


class PilotGlobalStop(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_once(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def verify_freeze() -> tuple[dict, dict, dict]:
    if not FROZEN.is_dir():
        raise PilotGlobalStop("frozen_project_pilot_missing")
    plan = read_json(FROZEN / "plan.json")
    authorization = read_json(FROZEN / "authorization.json")
    runtime = read_json(FROZEN / "runtime-config.json")
    seal = read_json(FROZEN / "freeze-seal.json")
    if authorization.get("approved") is not True or authorization.get("plan_sha256") != sha256_bytes(
        (FROZEN / "plan.json").read_bytes()
    ):
        raise PilotGlobalStop("frozen_authorization_mismatch")
    for relative, expected in seal.get("files", {}).items():
        path = FROZEN / relative
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected:
            raise PilotGlobalStop("frozen_input_seal_mismatch")
    for relative, expected in plan.get("runtime_hashes", {}).items():
        path = ROOT / relative
        if not path.is_file() or sha256_bytes(path.read_bytes()) != expected:
            raise PilotGlobalStop("frozen_runtime_hash_mismatch:" + relative)
    if runtime.get("model_requested") != "deepseek-flash":
        raise PilotGlobalStop("frozen_provider_model_mismatch")
    return plan, authorization, runtime


def read_provider_balance(*, api_key: str, expected_cny: Decimal, expected_usd: Decimal) -> dict:
    """Read the provider's account balance before any paid dispatch."""
    import httpx

    try:
        with httpx.Client(timeout=15, trust_env=False) as client:
            response = client.get(
                "https://api.deepseek.com/user/balance",
                headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        raise PilotGlobalStop("provider_balance_read_failed:" + type(exc).__name__) from exc
    if payload.get("is_available") is not True or not isinstance(payload.get("balance_infos"), list):
        raise PilotGlobalStop("provider_balance_unavailable")
    balances = {}
    for row in payload["balance_infos"]:
        currency = row.get("currency")
        amount = row.get("total_balance")
        if currency in {"USD", "CNY"}:
            try:
                value = Decimal(str(amount))
            except Exception as exc:
                raise PilotGlobalStop("provider_balance_malformed") from exc
            if not value.is_finite() or value < 0:
                raise PilotGlobalStop("provider_balance_malformed")
            balances[currency] = value
    enough = (("CNY" in balances and balances["CNY"] >= expected_cny)
              or ("USD" in balances and balances["USD"] >= expected_usd))
    if not enough:
        raise PilotGlobalStop("provider_balance_below_remaining_trial_reserve")
    return {
        "source": "GET https://api.deepseek.com/user/balance",
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "is_available": payload["is_available"],
        "balance_infos": payload["balance_infos"],
        "required_remaining_usd": str(expected_usd),
        "required_remaining_cny": str(expected_cny),
    }


def require_paid_authorization(plan: dict, authorization: dict, runtime: dict) -> dict:
    if (
        plan.get("authorized_hard_cap_usd") != "10.00"
        or authorization.get("max_total_usd") != "10.00"
        or authorization.get("new_budget_round") is not False
        or authorization.get("historical_r5_authorization_reused") is not False
        or authorization.get("prior_reserved_usd_included") != "0.0539520"
    ):
        raise PilotGlobalStop("frozen_cumulative_budget_authorization_mismatch")
    region = runtime.get("processing_region", "")
    if not region or "not verified" in region.lower() or os.getenv("PROJECT_REVIEW_REGION_VERIFIED") != "1":
        raise PilotGlobalStop("provider_region_not_verified_in_frozen_authorization")
    if os.getenv("INVESTIGATION_APPROVAL_REFERENCE", "").strip() != authorization.get("authorization_reference"):
        raise PilotGlobalStop("provider_approval_reference_missing")
    if "deepseek" not in {
        value.strip().lower() for value in os.getenv("INSIGHT_ENABLED_MODEL_PROVIDERS", "").split(",")
    }:
        raise PilotGlobalStop("deepseek_not_enabled_for_paid_runtime")
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise PilotGlobalStop("deepseek_api_key_missing")
    reservations = [row for row in read_jsonl(LEDGER) if row.get("event") == "reservation"]
    project_reserved = max(0, int(runtime["project_max_model_requests"]) - (len(reservations) - 5))
    balance = read_provider_balance(
        api_key=api_key,
        expected_usd=USD_PER_REQUEST * project_reserved,
        expected_cny=CNY_PER_REQUEST * project_reserved,
    )
    return balance


def append_ledger_reservation(*, metadata: dict, request_bytes: int) -> int:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            rows = [json.loads(line) for line in handle if line.strip()]
            if len(rows) == 0:
                raise PilotGlobalStop("historical_budget_ledger_missing")
            if any(not isinstance(row, dict) or "event" not in row for row in rows):
                raise PilotGlobalStop("budget_ledger_malformed")
            reservations = [row for row in rows if row.get("event") == "reservation"]
            prior_ids = {row.get("request_index") for row in rows if row.get("event") in {"receipt", "transport_error"}}
            if any(row.get("request_index") not in prior_ids for row in reservations):
                raise PilotGlobalStop("budget_ledger_has_unresolved_reservation")
            old_reserved = sum((Decimal(row["reserved_usd"]) for row in reservations), Decimal(0))
            new_project_count = len(reservations) - 5
            current_group_count = sum(
                row.get("pilot_case_key") == metadata["pilot_case_key"] for row in reservations
            )
            max_total = int(metadata["whole_round_max_requests_including_prior"])
            if len(reservations) >= max_total or current_group_count >= MAX_MODEL_REQUESTS_PER_RUN:
                raise PilotGlobalStop("whole_round_or_case_request_cap_reached")
            if new_project_count >= int(metadata["project_max_model_requests"]):
                raise PilotGlobalStop("project_request_cap_reached")
            if old_reserved + USD_PER_REQUEST > USD_HARD_CAP:
                raise PilotGlobalStop("whole_round_usd_hard_cap_reached")
            index = max((int(row.get("request_index", 0)) for row in rows), default=0) + 1
            record = {
                "event": "reservation",
                "request_index": index,
                "reserved_usd": str(USD_PER_REQUEST),
                "reserved_cny": str(CNY_PER_REQUEST),
                "request_bytes": request_bytes,
                "request_count_after_reservation": len(reservations) + 1,
                "pilot_case_key": metadata["pilot_case_key"],
                "phase": metadata["phase"],
                "task_id": metadata["task_id"],
                "blind_arm": metadata["blind_arm"],
                "actual_arm": metadata["actual_arm"],
                "request_sha256": metadata["request_sha256"],
                "dispatch_pending": True,
                "prior_reservations_included": 5,
            }
            handle.seek(0, os.SEEK_END)
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            return index
        except PilotGlobalStop:
            raise
        except Exception as exc:
            raise PilotGlobalStop("budget_ledger_write_failed:" + type(exc).__name__) from exc
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_ledger_event(event: dict) -> None:
    try:
        with LEDGER.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception as exc:
        raise PilotGlobalStop("budget_ledger_receipt_write_failed:" + type(exc).__name__) from exc


def _offline_output(payload: dict) -> dict:
    query = json.loads(payload["input"][1]["content"])
    observations = query.get("observations", [])
    seen_metric = any(item.get("data", {}).get("type") == "metric_snapshot" for item in observations)
    seen_text = any(item.get("data", {}).get("type") == "text_retrieval" for item in observations)
    tools = query.get("available_tools", [])
    if "metric_snapshot" in tools and not seen_metric:
        decision, tool = "call_tool", "metric_snapshot"
    elif "text_evidence" in tools and not seen_text:
        decision, tool = "call_tool", "text_evidence"
    else:
        decision, tool = "stop", None
    if decision == "call_tool":
        arguments = json.dumps({"query": "public review text", "evidence_context": "any"}, ensure_ascii=False)
        final_answer, stop_reason = None, None
    else:
        from app.agent.output_contract import FINAL_SCHEMA_VERSION
        from insight.investigation.business_schema import BUSINESS_FIELDS, BUSINESS_SCHEMA_VERSION

        arguments = "{}"
        final_answer = {
            "schema_version": FINAL_SCHEMA_VERSION,
            "statement": "离线结构预检输出；不包含语义评估。",
            "citations": [],
            "business_contract": {
                "schema_version": BUSINESS_SCHEMA_VERSION,
                **{field: "unknown" for field in BUSINESS_FIELDS},
            },
        }
        stop_reason = "offline_adapter_preflight"
    return {
        "decision": decision,
        "tool_name": tool,
        "arguments": arguments,
        "decision_summary": "Offline transport fixture; semantic quality is not evaluated.",
        "target_hypothesis_id": None,
        "stop_reason": stop_reason,
        "final_answer": final_answer,
        "confidence": None,
        "missing_fields": "",
        "counter_evidence_query": "",
    }


class ProjectRequestGuard:
    def __init__(self, *, mode: str, output_dir: Path, metadata: dict, balance: dict | None):
        self.mode = mode
        self.output_dir = output_dir
        self.metadata = metadata
        self.balance = balance
        self.count = 0
        self.stopped = ""
        self.receipts = []

    def client(self, **kwargs):
        if kwargs.get("max_retries") != 0 or str(kwargs.get("base_url", "")).rstrip("/") != "https://api.deepseek.com":
            raise PilotGlobalStop("unapproved_provider_client_configuration")
        actual = None
        if self.mode == "paid":
            import openai

            constructor = getattr(openai, "OpenAI_original_for_project_review", None)
            if constructor is None:
                raise PilotGlobalStop("original_provider_constructor_missing")
            actual = constructor(**kwargs)

        def create(**payload):
            if self.stopped:
                raise PilotGlobalStop("whole_round_circuit_open")
            wire = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_sha = sha256_bytes(wire)
            if (
                payload.get("model") != "deepseek-flash"
                or payload.get("reasoning") != {"effort": "none"}
                or payload.get("max_output_tokens") != MAX_OUTPUT_TOKENS
                or payload.get("temperature") != 0
                or payload.get("store") is not False
                or payload.get("text", {}).get("format", {}).get("name") != "agent_planner"
                or payload.get("text", {}).get("format", {}).get("strict") is not True
                or len(wire) + 1024 > MAX_REQUEST_BYTES
            ):
                self.stopped = "project_request_preflight_mismatch"
                raise PilotGlobalStop(self.stopped)
            from app.semantic.structured_response import validate_strict_schema

            validate_strict_schema(payload["text"]["format"]["schema"])
            self.count += 1
            file_index = len(self.receipts) + 1
            write_once(self.output_dir / f"request-{file_index:02}.json", payload)
            row = {**self.metadata, "request_sha256": request_sha, "pilot_case_key": self.metadata["pilot_case_key"]}
            if self.mode == "paid":
                try:
                    request_index = append_ledger_reservation(metadata=row, request_bytes=len(wire))
                except PilotGlobalStop as exc:
                    self.stopped = str(exc)
                    raise
                reservation = {
                    "event": "reservation",
                    "request_index": request_index,
                    "pilot_case_key": row["pilot_case_key"],
                    "request_sha256": request_sha,
                    "request_bytes": len(wire),
                    "reserved_usd": str(USD_PER_REQUEST),
                    "reserved_cny": str(CNY_PER_REQUEST),
                    "dispatch": True,
                }
            else:
                request_index = self.count
                reservation = {
                    "event": "offline_fixture",
                    "request_index": request_index,
                    "request_sha256": request_sha,
                    "request_bytes": len(wire),
                    "dispatch": False,
                }
            self.receipts.append(reservation)
            write_once(self.output_dir / f"reservation-{file_index:02}.json", reservation)
            if self.mode == "offline":
                result = _offline_output(payload)
                output_text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                raw = {
                    "id": f"offline-project-review-{request_index}",
                    "model": payload["model"],
                    "status": "completed",
                    "output_text": output_text,
                    "usage": {"input_tokens": 20, "output_tokens": 20, "total_tokens": 40},
                    "offline_substitute": True,
                }
                return SimpleNamespace(
                    status="completed",
                    id=raw["id"],
                    model=raw["model"],
                    output_text=output_text,
                    usage=SimpleNamespace(input_tokens=20, output_tokens=20),
                    incomplete_details=None,
                    model_dump=lambda **kwargs: raw,
                )
            try:
                response = actual.responses.create(**payload)
                raw = response.model_dump(mode="json")
                usage = raw.get("usage") or {}
                input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
                if (
                    response.status != "completed"
                    or type(input_tokens) is not int
                    or type(output_tokens) is not int
                    or min(input_tokens, output_tokens) < 0
                    or output_tokens > MAX_OUTPUT_TOKENS
                    or not isinstance(raw.get("model"), str)
                    or not raw["model"]
                    or not isinstance(raw.get("id"), str)
                    or not raw["id"]
                ):
                    raise PilotGlobalStop("provider_response_or_usage_incomplete")
                response_sha = sha256_bytes(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode())
                receipt = {
                    "event": "receipt",
                    "request_index": request_index,
                    "phase": row["phase"],
                    "task_id": row["task_id"],
                    "blind_arm": row["blind_arm"],
                    "actual_arm": row["actual_arm"],
                    "requested_model": payload["model"],
                    "returned_model": raw["model"],
                    "provider_response_id": raw["id"],
                    "request_sha256": request_sha,
                    "response_sha256": response_sha,
                    "request_bytes": len(wire),
                    "usage": usage,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "reserved_usd": str(USD_PER_REQUEST),
                    "reserved_cny": str(CNY_PER_REQUEST),
                    "conservative_peak_cost_usd_all_input_cache_miss": str(
                        (Decimal(input_tokens) * Decimal("0.30") + Decimal(output_tokens) * Decimal("1.20"))
                        / Decimal(1_000_000)
                    ),
                    "invoice_cost_available_from_response": False,
                    "response_status": response.status,
                }
                write_once(self.output_dir / f"response-{file_index:02}.json", raw)
                append_ledger_event(receipt)
                return response
            except Exception as exc:
                self.stopped = self.stopped or "provider_transport_or_protocol_failure"
                failed = {
                    "event": "transport_error",
                    "request_index": request_index,
                    "phase": row["phase"],
                    "task_id": row["task_id"],
                    "blind_arm": row["blind_arm"],
                    "actual_arm": row["actual_arm"],
                    "request_sha256": request_sha,
                    "error_type": type(exc).__name__,
                    "http_status": getattr(exc, "status_code", None),
                    "reservation_retained": True,
                    "charge_outcome": "unknown_until_provider_receipt",
                }
                try:
                    write_once(self.output_dir / f"error-{file_index:02}.json", failed)
                    append_ledger_event(failed)
                except PilotGlobalStop:
                    pass
                raise

        return SimpleNamespace(responses=SimpleNamespace(create=create))

    def offline_plan(self, *, system: str, user: str, schema: dict) -> str:
        from app.semantic.structured_response import structured_request

        payload = structured_request(
            model="deepseek-flash", effort="none", name="agent_planner", system=system,
            user=user, schema=schema, max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        wire = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.count += 1
        file_index = len(self.receipts) + 1
        if self.count > MAX_MODEL_REQUESTS_PER_RUN or len(wire) + 1024 > MAX_REQUEST_BYTES:
            self.stopped = "offline_project_request_bound_failed"
            raise PilotGlobalStop(self.stopped)
        write_once(self.output_dir / f"request-{file_index:02}.json", payload)
        output_text = json.dumps(_offline_output(payload), ensure_ascii=False, separators=(",", ":"))
        receipt = {
            "event": "offline_fixture", "request_index": self.count,
            "request_sha256": sha256_bytes(wire), "request_bytes": len(wire),
            "dispatch": False, "offline_substitute": True,
        }
        self.receipts.append(receipt)
        write_once(self.output_dir / f"reservation-{file_index:02}.json", receipt)
        write_once(self.output_dir / f"response-{file_index:02}.json", {
            "status": "completed", "model": "deepseek-flash", "output_text": output_text,
            "usage": {"input_tokens": 20, "output_tokens": 20, "total_tokens": 40},
            "offline_substitute": True,
        })
        return output_text


def _load_tasks(phase: str) -> list[dict]:
    if phase == "smoke":
        return read_jsonl(FROZEN / "smoke-tasks.jsonl")
    if phase == "formal":
        return read_jsonl(FROZEN / "formal-tasks.jsonl")
    raise PilotGlobalStop("invalid_pilot_phase")


def _execution_root(mode: str, plan: dict | None = None) -> Path:
    if plan is None:
        plan, _, _ = verify_freeze()
    return ARTIFACTS / "execution" / f"{mode}-freeze-v{plan['freeze_revision']}"


def pytest_generate_tests(metafunc):
    if "pilot_case" not in metafunc.fixturenames:
        return
    if MODE not in {"offline", "paid"}:
        metafunc.parametrize("pilot_case", [pytest.param(None, id="pilot-disabled")])
        return
    verify_freeze()
    tasks = {row["task_id"]: row for row in _load_tasks(PHASE)}
    order_name = "smoke-order.jsonl" if PHASE == "smoke" else "formal-order.jsonl"
    order = read_jsonl(FROZEN / order_name)
    cases = [(tasks[row["task_id"]], row["blind_arm"]) for row in order]
    metafunc.parametrize("pilot_case", cases, ids=[f"{PHASE}-{task['task_id']}-{arm}" for task, arm in cases])


def _configure_offline_environment(monkeypatch):
    monkeypatch.setenv("INSIGHT_ENABLED_MODEL_PROVIDERS", "deepseek")
    monkeypatch.setenv("INVESTIGATION_APPROVAL_REFERENCE", "offline-project-pilot-preflight")
    monkeypatch.setenv("INVESTIGATION_REGION", "offline-no-provider-dispatch")
    monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "USD")
    monkeypatch.setenv("INVESTIGATION_INPUT_COST_PER_MILLION", "0.30")
    monkeypatch.setenv("INVESTIGATION_OUTPUT_COST_PER_MILLION", "1.20")
    monkeypatch.setenv("INVESTIGATION_MAX_CALL_COST", str(USD_PER_REQUEST))
    monkeypatch.setenv("INVESTIGATION_MAX_RUN_COST", str(USD_PER_REQUEST * MAX_STEPS))
    plan, authorization, runtime = verify_freeze()
    evaluation_max = USD_PER_REQUEST * MAX_STEPS * (
        2 * (plan["task_counts"]["formal"] + plan["task_counts"]["smoke"])
    )
    monkeypatch.setenv("INVESTIGATION_MAX_EVAL_COST", str(min(evaluation_max, USD_HARD_CAP - Decimal(authorization["prior_reserved_usd_included"]))))
    monkeypatch.setenv("INVESTIGATION_MAX_REQUEST_BYTES", str(MAX_REQUEST_BYTES))
    monkeypatch.setenv("INVESTIGATION_MAX_OUTPUT_TOKENS", str(MAX_OUTPUT_TOKENS))
    monkeypatch.setenv("INVESTIGATION_MAX_TOOL_ACTIONS", str(MAX_MODEL_ACTIONS))
    monkeypatch.setenv("INVESTIGATION_MODEL", "deepseek-flash")
    monkeypatch.setenv("INVESTIGATION_REASONING_EFFORT", "none")
    monkeypatch.setenv("INVESTIGATION_PLANNER_PROTOCOL", "legacy-v1")
    monkeypatch.setenv("STRUCTURED_RESPONSE_TEMPERATURE", "0")
    monkeypatch.setenv("INVESTIGATION_PLANNER_PROVIDER", "deterministic")
    monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "0")
    monkeypatch.setenv("EVIDENCE_CANDIDATE_MODE", "lexical")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "offline-placeholder-not-a-credential")


def _prepare_review_dataset(actor, shop, task: dict, corpus: dict[str, dict], storage_root: Path) -> dict:
    from django.core.files.uploadedfile import SimpleUploadedFile

    from insight.ingestion.service import accept_upload, create_source, publish_batch
    from insight.models import Product, ProductMapping

    asin = task["fixture_asin"]
    review_rows = [corpus[record_id] for record_id in task["source_record_ids"]]
    product = Product.objects.create(
        shop=shop,
        child_asin=asin,
        seller_sku=asin + "-SYNTHETIC",
        fulfillment="FBA",
    )
    ProductMapping.objects.create(
        product=product,
        version=1,
        name="合成隔离评测对象（不是 UCI 源数据商品）",
        effective_from=date(2017, 1, 1),
        reason="Local validation mapping only; UCI source contains no product metadata.",
    )
    fields = [
        "child_asin", "seller_sku", "fulfillment", "business_date", "record_id", "title", "text",
        "rating", "language", "collected_at",
    ]
    rows = []
    source_kinds = {}
    for item in review_rows:
        source_kinds[item["record_id"]] = item["source_kind"]
        rows.append({
            "child_asin": asin,
            "seller_sku": product.seller_sku,
            "fulfillment": product.fulfillment,
            "business_date": task["fixture_business_date"],
            "record_id": item["record_id"],
            "title": "",
            "text": item["text"],
            # The project Review schema requires these fields; the retrieval
            # adapter exposes only title/text. The value is synthetic metadata.
            "rating": "3",
            "language": "en",
            "collected_at": task["fixture_business_date"] + "T12:00:00-07:00",
        })
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    mapping = {field: field for field in fields}
    request_id = "real-project-review-pilot-" + task["task_id"]
    source = create_source(actor, shop, {
        "name": "隔离公开评论 pilot " + task["task_id"],
        "kind": "reviews",
        "is_primary": True,
        "grain": "product_record",
        "coverage_basis": "business_date",
        "column_mapping": mapping,
        "ignored_columns": [],
    }, request_id)
    batch = accept_upload(
        actor,
        source,
        SimpleUploadedFile("public-review-pilot.csv", stream.getvalue().encode("utf-8")),
        {
            "coverage_start": date.fromisoformat(task["fixture_business_date"]),
            "coverage_end": date.fromisoformat(task["fixture_business_date"]),
            "generated_at": datetime.fromisoformat(
                task["fixture_business_date"] + "T12:00:00-07:00"
            ),
            "declared_complete": False,
            "declared_asins": [asin],
        },
        request_id,
    )
    from insight.models import Job
    from insight.tasks import run_job

    parse_job = Job.objects.get(kind="parse_import", payload__batch_id=str(batch.pk))
    run_job(str(parse_job.pk))
    parse_job.refresh_from_db()
    if parse_job.status != "completed":
        raise PilotGlobalStop("isolated_public_review_parse_job_failed")
    batch.refresh_from_db()
    if batch.error_rows or batch.valid_rows != len(review_rows):
        raise PilotGlobalStop("isolated_public_review_import_failed")
    publish_batch(actor, batch.pk, allow_partial=False, confirm_contract=True, request_id=request_id)
    return {
        "source_id": str(source.pk),
        "batch_id": str(batch.pk),
        "product_id": str(product.pk),
        "fixture_asin": asin,
        "fixture_business_date": task["fixture_business_date"],
        "records": [{"record_id": row["record_id"], "source_kind": row["source_kind"], "text": row["text"]} for row in review_rows],
        "source_kind_counts": dict(Counter(source_kinds.values())),
        "rating_field": "synthetic required-schema placeholder; not passed in retrieved evidence",
        "mapping_statement": "ASIN/date/SKU/FBA mapping is synthetic and exists only in this Django test database.",
        "storage_root": str(storage_root),
    }


def _update_blind_review_bundle(mode: str, phase: str) -> None:
    output_root = _execution_root(mode)
    tasks = {row["task_id"]: row for row in _load_tasks(phase)}
    corpus = {row["record_id"]: row for row in read_jsonl(FROZEN / "review-corpus.jsonl")}
    order_name = "smoke-order.jsonl" if phase == "smoke" else "formal-order.jsonl"
    order = read_jsonl(FROZEN / order_name)
    pair_order = {}
    for row in order:
        pair_order.setdefault(row["task_id"], []).append(row["blind_arm"])
    package = []
    case_number = 0
    for task_id, blind_arms in pair_order.items():
        files = [output_root / f"{phase}-{task_id}-{arm}.json" for arm in blind_arms]
        if not all(path.is_file() for path in files):
            continue
        case_number += 1
        task = tasks[task_id]
        candidates = []
        for label, path in zip(("A", "B"), files):
            result = read_json(path)
            candidates.append({
                "candidate": label,
                "status": result.get("status"),
                "project_output": result.get("project_output"),
                "run_status": result.get("run_status"),
                "failure_class": result.get("failure_class"),
            })
        package.append({
            "review_case_id": f"{phase[:1].upper()}-{case_number:03d}",
            "question": task["question"],
            "synthetic_scope_notice": task["api_query"].split("仅基于可引用原文回答")[0],
            "source_records": [
                {"record_id": record_id, "source_kind": corpus[record_id]["source_kind"], "text": corpus[record_id]["text"]}
                for record_id in task["source_record_ids"]
            ],
            "candidates": candidates,
            "independent_review": {
                "citation_support": "",
                "scope_fit": "",
                "balanced_summary": "",
                "instruction_injection_handling": "",
                "unsupported_claims": "",
                "reviewer_note": "",
            },
        })
    target = output_root / f"blind-review-bundle-{phase}.jsonl"
    temp = target.with_suffix(".jsonl.tmp")
    temp.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in package), encoding="utf-8")
    os.replace(temp, target)
    write_path = output_root / f"blind-review-bundle-{phase}.status.json"
    status = {
        "status": "awaiting_independent_human_review" if package else "no_complete_blinded_pairs_yet",
        "complete_pairs": len(package),
        "expected_pairs": len(tasks),
        "human_labels": 0,
        "quality_accepted": False,
        "bundle_sha256": sha256_bytes(target.read_bytes()),
    }
    tmp_status = write_path.with_suffix(".json.tmp")
    tmp_status.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp_status, write_path)


def test_real_project_review_pilot_runtime(setup_scope, settings, monkeypatch, pilot_case):
    if pilot_case is None:
        pytest.skip("explicit project review pilot mode required")
    task, blind_arm = pilot_case
    plan, authorization, runtime = verify_freeze()
    arm_map = read_json(FROZEN / "private/blind-arm-map.json")
    actual_arm = arm_map[f"{PHASE}:{task['task_id']}"][blind_arm]
    mode_root = _execution_root(MODE, plan)
    mode_root.mkdir(parents=True, exist_ok=True)
    output_path = mode_root / f"{PHASE}-{task['task_id']}-{blind_arm}.json"
    if output_path.exists():
        pytest.fail("refusing to repeat an existing pilot dispatch: " + output_path.name)

    if MODE == "paid" and PHASE == "formal":
        smoke_dir = _execution_root("paid", plan)
        smoke_files = [smoke_dir / f"smoke-{task_id}-{arm}.json" for task_id in ("S01", "S02", "S03") for arm in ("A", "B")]
        try:
            if not all(path.is_file() for path in smoke_files):
                raise PilotGlobalStop("formal_phase_locked_until_all_paid_smoke_pairs_finish")
            if any(read_json(path).get("failure_class") == "global_stop" for path in smoke_files):
                raise PilotGlobalStop("formal_phase_locked_by_paid_smoke_global_stop")
            smoke_status = read_json(smoke_dir / "blind-review-bundle-smoke.status.json")
            if smoke_status.get("complete_pairs") != 3:
                raise PilotGlobalStop("paid_smoke_pair_bundle_incomplete")
        except PilotGlobalStop as exc:
            write_once(output_path, {
                "status": "not_started",
                "phase": PHASE,
                "task_id": task["task_id"],
                "blind_arm": blind_arm,
                "failure_class": "authorization_or_preflight_global_stop",
                "failure": str(exc),
                "provider_requests_sent": 0,
            })
            pytest.fail(str(exc))

    balance = None
    if MODE == "paid":
        try:
            global _PAID_BALANCE_RECEIPT
            if _PAID_BALANCE_RECEIPT is None:
                _PAID_BALANCE_RECEIPT = require_paid_authorization(plan, authorization, runtime)
                write_once(mode_root / "provider-balance-before-paid-round.json", _PAID_BALANCE_RECEIPT)
            balance = _PAID_BALANCE_RECEIPT
        except PilotGlobalStop as exc:
            write_once(output_path, {
                "status": "not_started",
                "phase": PHASE,
                "task_id": task["task_id"],
                "blind_arm": blind_arm,
                "failure_class": "authorization_or_preflight_global_stop",
                "failure": str(exc),
                "provider_requests_sent": 0,
            })
            pytest.fail(str(exc))
    else:
        _configure_offline_environment(monkeypatch)

    if MODE == "paid":
        monkeypatch.setenv("INSIGHT_ENABLED_MODEL_PROVIDERS", "deepseek")
        monkeypatch.setenv("INVESTIGATION_APPROVAL_REFERENCE", authorization["authorization_reference"])
        monkeypatch.setenv("INVESTIGATION_REGION", runtime["processing_region"])
        monkeypatch.setenv("INVESTIGATION_COST_CURRENCY", "USD")
        monkeypatch.setenv("INVESTIGATION_INPUT_COST_PER_MILLION", "0.30")
        monkeypatch.setenv("INVESTIGATION_OUTPUT_COST_PER_MILLION", "1.20")
        monkeypatch.setenv("INVESTIGATION_MAX_CALL_COST", str(USD_PER_REQUEST))
        monkeypatch.setenv("INVESTIGATION_MAX_RUN_COST", str(USD_PER_REQUEST * MAX_STEPS))
        monkeypatch.setenv("INVESTIGATION_MAX_EVAL_COST", str(USD_HARD_CAP - Decimal(authorization["prior_reserved_usd_included"])))

    output_dir = mode_root / "requests" / f"{PHASE}-{task['task_id']}-{blind_arm}"
    output_dir.mkdir(parents=True, exist_ok=False)
    metadata = {
        "pilot_case_key": f"{PHASE}:{task['task_id']}:{blind_arm}",
        "phase": PHASE,
        "task_id": task["task_id"],
        "blind_arm": blind_arm,
        "actual_arm": actual_arm,
        "whole_round_max_requests_including_prior": runtime["whole_round_max_model_requests_including_prior"],
        "project_max_model_requests": runtime["project_max_model_requests"],
    }
    guard = ProjectRequestGuard(mode=MODE, output_dir=output_dir, metadata=metadata, balance=balance)
    try:
        import openai

        if not hasattr(openai, "OpenAI_original_for_project_review"):
            openai.OpenAI_original_for_project_review = openai.OpenAI
        monkeypatch.setattr(openai, "OpenAI", guard.client)
        monkeypatch.setenv("STRUCTURED_RESPONSE_TEMPERATURE", "0")
        monkeypatch.setenv("INVESTIGATION_MODEL", "deepseek-flash")
        monkeypatch.setenv("INVESTIGATION_REASONING_EFFORT", "none")
        monkeypatch.setenv("INVESTIGATION_MAX_OUTPUT_TOKENS", str(MAX_OUTPUT_TOKENS))
        monkeypatch.setenv("INVESTIGATION_MAX_REQUEST_BYTES", str(MAX_REQUEST_BYTES))
        monkeypatch.setenv("INVESTIGATION_MAX_TOOL_ACTIONS", str(MAX_MODEL_ACTIONS))
        monkeypatch.setenv("EVIDENCE_RELEVANCE_ENABLED", "0")
        monkeypatch.setenv("EVIDENCE_CANDIDATE_MODE", "lexical")
        monkeypatch.setenv("INVESTIGATION_PLANNER_PROTOCOL", "legacy-v1")

        from rest_framework.test import APIClient

        from app.agent.planner import LLMPlanner
        from app.semantic import factory
        from insight.investigation.planner import ModelPlanner, _external_planner
        from insight.models import InvestigationBudgetLedger, InvestigationRun, Job, Product
        from insight.tasks import run_job

        if MODE == "offline":
            class OfflineGateway:
                provenance = "offline-project-review-preflight"
                execution_origin = "provider_response"
                last_usage = {"input_tokens": 20, "output_tokens": 20}
                last_model_receipt = {"provider": "offline-fixture", "response_received": True}

                def plan(self, *, system, user, schema):
                    return guard.offline_plan(system=system, user=user, schema=schema)

            monkeypatch.setattr(factory, "build_agent_planner", lambda: LLMPlanner(OfflineGateway()))

        _external_planner.cache_clear()

        original_decide = ModelPlanner.decide
        fixed_route = ("metric_snapshot", "text_evidence")

        def bounded_project_decide(self, *, query, evidence, history, available_tools, request_context=None):
            action_count = sum(bool(item.get("tool")) for item in history)
            offered = list(available_tools) if action_count < MAX_MODEL_ACTIONS else []
            decision = original_decide(
                self,
                query=query,
                evidence=evidence,
                history=history,
                available_tools=offered,
                request_context=request_context,
            )
            if actual_arm == "fixed" and not decision.clarification:
                target = fixed_route[action_count] if action_count < len(fixed_route) else ""
                if target and target not in available_tools:
                    raise PilotGlobalStop("fixed_route_tool_not_available")
                decision = replace(decision, next_tool=target)
            return decision

        monkeypatch.setattr(ModelPlanner, "decide", bounded_project_decide)

        users, shops = setup_scope
        shop = shops[0]
        from insight.models import WorkspaceModelBudgetPolicy

        WorkspaceModelBudgetPolicy.objects.create(
            workspace=shop.workspace,
            enabled=True,
            currency="USD",
            daily_cost_limit=Decimal("1.00"),
            monthly_cost_limit=Decimal("10.00"),
            max_concurrent_external=1,
            lease_seconds=300,
            updated_by=users["admin"],
        )
        settings.STORAGE_ROOT = mode_root / "storage"
        settings.STORAGE_ROOT.mkdir(parents=True, exist_ok=True)
        corpus_rows = read_jsonl(FROZEN / "review-corpus.jsonl")
        corpus = {row["record_id"]: row for row in corpus_rows}
        imported = _prepare_review_dataset(users["admin"], shop, task, corpus, settings.STORAGE_ROOT)
        product = Product.objects.get(shop=shop, child_asin=task["fixture_asin"])
        api = APIClient()
        api.force_authenticate(users["admin"])
        response = api.post("/api/v1/investigations/", {
            "shop": str(shop.pk),
            "query": task["api_query"],
            "child_asin": task["fixture_asin"],
            "product": str(product.pk),
            "max_steps": MAX_STEPS,
            "allow_internal_task": False,
            "planner_provider": "deepseek",
            "idempotency_key": f"real-project-review-v{plan['freeze_revision']}-{PHASE}-{task['task_id']}-{blind_arm}",
        }, format="json")
        if response.status_code != 202:
            raise PilotGlobalStop("project_investigation_api_rejected:" + str(response.status_code))
        job_id = response.data["id"]
        run_id = response.data["result"]["investigation_id"]
        guard.metadata["job_id"] = str(job_id)
        guard.metadata["run_id"] = str(run_id)
        run_job(str(job_id))
        job = Job.objects.get(pk=job_id)
        run = InvestigationRun.objects.get(pk=run_id)
        ledgers = list(InvestigationBudgetLedger.objects.filter(run=run, kind="model").order_by("sequence"))
        failure_codes = [
            row.metadata.get("failure_code") or row.metadata.get("error")
            for row in ledgers if isinstance(row.metadata, dict) and (row.metadata.get("failure_code") or row.metadata.get("error"))
        ]
        local_failure = next((code for code in failure_codes if code in LOCAL_OUTPUT_FAILURE_CODES), None)
        tool_calls = list(run.tool_calls.order_by("sequence").values(
            "sequence", "tool_name", "authorization", "status", "error", "output_refs"
        ))
        if len(tool_calls) > MAX_MODEL_ACTIONS or any(
            item["tool_name"] in {"create_draft_task", "write_task"} or item["authorization"] != "allowed"
            for item in tool_calls
        ):
            guard.stopped = "project_tool_action_or_authorization_bound_failed"
        if actual_arm == "fixed" and run.status == "completed" and [row["tool_name"] for row in tool_calls] != [
            "metric_snapshot", "text_evidence"
        ]:
            guard.stopped = "fixed_arm_route_receipt_mismatch"
        if guard.stopped:
            failure_class = "global_stop"
        elif local_failure:
            failure_class = "case_local_output_failure"
        elif run.status == "completed" and job.status == "completed":
            failure_class = "none"
        elif run.status == "needs_clarification":
            failure_class = "case_local_answer_or_scope_failure"
        elif run.status == "partial" and run.stop_reason in {
            "unsupported_text_evidence_arguments", "invalid_text_evidence_query", "invalid_text_evidence_context",
            "duplicate_text_evidence_request", "planner_contract_invalid",
        }:
            failure_class = "case_local_output_failure"
        else:
            failure_class = "global_stop"
            if not guard.stopped:
                guard.stopped = "project_job_or_worker_failure"

        run_payload = {
            "status": "completed" if failure_class == "none" else "case_failed" if failure_class.startswith("case_local") else "global_stop",
            "phase": PHASE,
            "task_id": task["task_id"],
            "blind_arm": blind_arm,
            "actual_arm": actual_arm,
            "failure_class": failure_class,
            "failure_codes": failure_codes,
            "failure": guard.stopped or run.stop_reason or job.error or "",
            "provider_requests_sent": sum(bool(item.get("dispatch")) for item in guard.receipts),
            "provider_reservations": guard.receipts,
            "job": {"id": str(job.pk), "status": job.status, "error": job.error, "result": job.result},
            "run_id": str(run.pk),
            "run_status": run.status,
            "run_stop_reason": run.stop_reason,
            "steps": list(run.steps.values("sequence", "node", "tool_name", "status", "evidence_refs")),
            "decisions": run.decisions,
            "tool_calls": tool_calls,
            "budget": {
                "model_attempts": run.model_attempts,
                "model_cost": str(run.model_cost),
                "durable_model_entries": len(ledgers),
            },
            "input_import": imported,
            "project_output": run.result,
            "human_review_status": "pending_independent_human_review",
        }
        write_once(output_path, run_payload)
        if failure_class == "global_stop":
            pytest.fail(guard.stopped or "project_review_global_stop")
        _update_blind_review_bundle(MODE, PHASE)
    except PilotGlobalStop as exc:
        guard.stopped = str(exc)
        if not output_path.exists():
            write_once(output_path, {
                "status": "global_stop",
                "phase": PHASE,
                "task_id": task["task_id"],
                "blind_arm": blind_arm,
                "actual_arm": actual_arm,
                "failure_class": "global_stop",
                "failure": str(exc),
                "provider_requests_sent": sum(bool(item.get("dispatch")) for item in guard.receipts),
            })
        pytest.fail(str(exc))
    except Exception as exc:
        if not output_path.exists():
            write_once(output_path, {
                "status": "global_stop",
                "phase": PHASE,
                "task_id": task["task_id"],
                "blind_arm": blind_arm,
                "actual_arm": actual_arm,
                "failure_class": "global_stop",
                "failure": type(exc).__name__ + ": " + str(exc)[:300],
                "provider_requests_sent": sum(bool(item.get("dispatch")) for item in guard.receipts),
            })
        pytest.fail("project_pilot_unclassified_system_failure:" + type(exc).__name__)
