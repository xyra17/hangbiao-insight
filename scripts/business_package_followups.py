"""Public-API lifecycle/action acceptance for this run's fully imported packages."""
import csv
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from business_package_oracle import read_rows
from run_business_package_acceptance import (
    ROOT,
    checked,
    import_file,
    manage,
    save,
    wait_job,
)


def queued(client, path, payload):
    return wait_job(client, checked(client, "POST", path, json=payload)["id"])


def artifacts(client, shop, asin, prefix, *, investigation_query="核对冻结指标、分母和来源，缺源时停止判断原因"):
    semantic = queued(client, "/api/v1/semantic-runs/", {"shop": shop, "child_asin": asin, "provider": "deterministic", "idempotency_key": prefix + "-report"})
    report = checked(client, "GET", f"/api/v1/reports/{semantic['result']['report_id']}/")
    investigation = queued(client, "/api/v1/investigations/", {"shop": shop, "child_asin": asin, "query": investigation_query, "max_steps": 6, "idempotency_key": prefix + "-investigation"})
    investigation = checked(client, "GET", f"/api/v1/investigations/{investigation['result']['investigation_id']}/")
    return {"report": report, "investigation": investigation}


def lifecycle(client, env, output, pack, identities, as_of):
    main_state = json.loads((output / "state.json").read_text())
    path = output / "lifecycle-state.json"
    state = json.loads(path.read_text()) if path.exists() else {"pack": "04", "run_id": main_state["run_id"], "sources": main_state["sources"].copy(), "files": {}, "steps": []}
    shop, asin = identities["shop_id"], "B07H2V5YLH"
    if not state.get("original_artifacts"):
        state["original_artifacts"] = artifacts(client, shop, asin, state["run_id"] + "-before-lifecycle")
        snapshot = main_state["snapshots"][asin]
        members = checked(client, "GET", f"/api/v1/shops/{shop}/members/")
        owner_id = next(u["id"] for u in members["items"] if u["username"] == "accept_admin")
        created = checked(client, "POST", "/api/v1/action-tasks/", json={"shop": shop, "child_asin": asin, "assigned_to": owner_id, "title": "[SYNTHETIC] 04修订前冻结动作", "due_at": as_of+"T23:00:00-07:00", "question": "核对历史来源版本", "completion_criteria": "不覆盖旧快照与动作", "observed_metrics": ["ordered_units"], "evidence_refs": [f"snapshot:{snapshot}"], "is_formal": True})
        state["original_action"] = created
        save(path, state)
    revision = pack / "import_ready/lifecycle_revisions"
    for filename in ["sales_traffic_revision_v1.csv", "sales_traffic_revision_v2.csv", "refund_late_arrival.csv", "returns_requested_v1.csv", "returns_received_v2.csv", "returns_cancelled_v3.csv", "order_retraction_candidate.csv"]:
        batch = import_file(client, revision / filename, shop, state, path)
        state["steps"].append({"filename": filename, "batch_id": batch["id"], "revision": batch["published_revision"], "expected": "full row publication", "actual": batch["valid_rows"]})
        save(path, state)
    candidate = state["files"][str(revision / "order_retraction_candidate.csv")]["batch_id"]
    revoke = queued(client, f"/api/v1/imports/{candidate}/revoke-publication/", {"reason": "隔离验收撤销候选，不删除历史事实", "idempotency_key": state["run_id"] + "-retract"})
    state["retraction_job"] = revoke
    # Separate invalid batches never enter the main package publication.
    invalid_specs = [("orders_invalid_v1.csv", "orders", "child_asin"), ("orders_duplicate_v1.csv", "orders", "重复"),
                     ("refunds_invalid_v1.csv", "refunds", "currency"), ("returns_invalid_v1.csv", "returns", "收到日期")]
    boundaries = []
    for filename, kind, fragment in invalid_specs:
        meta = json.loads(Path(next(k for k in main_state["files"] if json.loads(Path(k+".meta.json").read_text())["source_payload"]["kind"] == kind)+".meta.json").read_text())
        payload = {**meta["source_payload"], "shop": shop, "name": f"Release invalid {filename} {state['run_id']}", "is_primary": False}
        listed = checked(client, "GET", "/api/v1/sources/", params={"shop": shop, "page_size": 100})
        existing = next((s for s in listed["results"] if s["name"] == payload["name"]), None)
        source = existing or checked(client, "POST", "/api/v1/sources/", json=payload)
        with (pack / "boundary_cases" / filename).open("rb") as handle:
            batch = checked(client, "POST", f"/api/v1/sources/{source['id']}/upload/", data={"coverage_start": "2023-01-01", "coverage_end": "2023-01-01", "generated_at": "2026-10-01T00:00:00Z", "declared_complete": "false", "declared_asins": ["B07P9S6R9Y"]}, files={"file": (filename, handle, "text/csv")})
        for _ in range(120):
            batch = checked(client, "GET", f"/api/v1/imports/{batch['id']}/")
            if batch["status"] == "invalid":
                break
            time.sleep(0.5)
        errors = checked(client, "GET", f"/api/v1/imports/{batch['id']}/rows/", params={"errors": 1})
        assert batch["status"] == "invalid" and batch["error_rows"] > 0
        assert any(fragment in e["error"] for e in errors["results"]), errors
        blocked_job = checked(client, "POST", f"/api/v1/imports/{batch['id']}/publish/", json={"allow_partial": False, "confirm_contract": True})
        for _ in range(120):
            actual = checked(client, "GET", f"/api/v1/jobs/{blocked_job['id']}/")
            if actual["status"] == "failed":
                break
            time.sleep(0.5)
        assert actual["status"] == "failed"
        boundaries.append({"filename": filename, "batch": batch, "errors": errors, "publish_job_status": actual["status"]})
    empty = import_file(client, pack / "import_ready/refunds_empty_report_v1.csv", shop, state, path, source_key="explicit-empty", primary=False)
    assert empty["total_rows"] == 0 and empty["coverage_status"] == "ready"
    boundaries.append({"filename": "refunds_empty_report_v1.csv", "batch": empty, "expected": "explicit zero report ready, distinct from missing/partial"})
    save(output / "boundary-results.json", boundaries)
    # Immutable old content must remain readable after versions and background refresh.
    original = state["original_artifacts"]
    old_report = checked(client, "GET", f"/api/v1/reports/{original['report']['id']}/")
    old_run = checked(client, "GET", f"/api/v1/investigations/{original['investigation']['id']}/")
    old_action = checked(client, "GET", f"/api/v1/action-tasks/{state['original_action']['id']}/")
    assert old_report["payload"] == original["report"]["payload"] and old_report["metric_snapshot"] == original["report"]["metric_snapshot"]
    assert old_run["result"] == original["investigation"]["result"]
    assert old_action["evidence_refs"] == state["original_action"]["evidence_refs"]
    save(output / "old-artifacts-preserved.json", {"report_id": old_report["id"], "investigation_id": old_run["id"], "action_id": old_action["id"], "checks": "frozen content and bindings unchanged"})
    save(path, state)
    manage(env, ["audit_release_lifecycle", "--state", str(path), "--output", str(output / "lifecycle-db-audit.json")], output / "audit.log")
    audit = json.loads((output / "lifecycle-db-audit.json").read_text())
    assert audit["latest_sales_sessions"] == 900 and audit["return_status"] == "cancelled" and audit["return_history_count"] == 3
    assert audit["retraction_count"] == 1 and audit["retracted_original_retained"] and Decimal(audit["late_refund_amount"]) == Decimal("3.50")
    save(output / "lifecycle-result.json", {"status": "passed", "scope": "full seven lifecycle files, four invalid boundaries, explicit empty, real worker, immutable original artifacts and DB versions", "actual": audit, "source_nature": "synthetic_revision_fixture"})


def action_oracle(rows, asin, effective_date):
    effective = date.fromisoformat(effective_date)
    selected = {(r["child_asin"], r["business_date"]): r for r in rows}
    baseline = [effective - timedelta(days=i) for i in range(28, 0, -1)]
    current = [effective + timedelta(days=i) for i in range(1, 8)]
    b = sum(int(selected[(asin, str(day))]["ordered_units"]) for day in baseline)
    c = sum(int(selected[(asin, str(day))]["ordered_units"]) for day in current)
    bm, cm = (Decimal(b) / 28).quantize(Decimal("0.000001")), (Decimal(c) / 7).quantize(Decimal("0.000001"))
    return {"baseline_total": str(b), "current_total": str(c), "baseline": str(bm), "current": str(cm), "delta": str(cm-bm),
            "baseline_start": str(baseline[0]), "baseline_end": str(baseline[-1]), "current_start": str(current[0]), "current_end": str(current[-1])}


def _set_business_clock(output, day, reason):
    save(output / "business-clock.json", {
        "as_of": day,
        "changed_at_real_utc": datetime.now(UTC).isoformat(),
        "reason": reason,
    })


def _relogin_acceptance_admin(client, output):
    session = checked(client, "GET", "/api/v1/session/")
    account = next(a for a in json.loads((output / "accounts.json").read_text()) if a["username"] == "accept_admin")
    login = checked(client, "POST", "/api/v1/login/", json=account, headers={"X-CSRFToken": session["csrf_token"]})
    client.headers["X-CSRFToken"] = login["csrf_token"]


def _write_sales_slice(source_path, target_path, *, start, end, change_last_units=False):
    with source_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        rows = [row for row in reader if start <= row["business_date"] <= end and row["child_asin"] == "D5X2600005"]
    if change_last_units and rows:
        rows[-1]["ordered_units"] = str(int(rows[-1]["ordered_units"]) + 1)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def _write_sales_metadata(path, shop_id, name, start, end, generated_at, template_path, *, asin="D5X2600005"):
    source_meta = json.loads(template_path.read_text())
    source_payload = {**source_meta["source_payload"], "shop": shop_id, "name": name, "is_primary": True}
    upload_payload = {"coverage_start": start, "coverage_end": end, "generated_at": generated_at,
                      "declared_complete": True, "declared_asins": [asin]}
    metadata = {"source_payload": source_payload, "upload_payload": upload_payload}
    Path(str(path) + ".meta.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2))


def _write_complete_forward_sales_slice(
    source_path,
    target_path,
    *,
    start,
    end,
    target_asin,
    change_last_units=False,
):
    """Build a complete daily timeline, labeling any generated day as synthetic."""
    start_day, end_day = date.fromisoformat(start), date.fromisoformat(end)
    with source_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames
        source_rows = [
            row for row in reader
            if row["child_asin"] == "D5X2600005" and start <= row["business_date"] <= end
        ]
    by_date = {row["business_date"]: row for row in source_rows}
    day = start_day
    rows = []
    while day <= end_day:
        key = str(day)
        if key in by_date:
            row = dict(by_date[key])
        else:
            if not rows:
                raise AssertionError(f"No source row available to construct {key}")
            row = dict(rows[-1])
            row["business_date"] = key
            row["source_reference"] = "synthetic_derived_from_local_acceptance_fixture"
        row["child_asin"] = target_asin
        row["source_row_id"] = f"complete-forward:{target_asin}:{key}"
        row["provenance_class"] = "synthetic_complete_forward_acceptance_fixture"
        row["is_real"] = "false"
        row["is_derived"] = "true"
        row["is_synthetic"] = "true"
        row["source_reference"] = "synthetic_derived_from_local_acceptance_fixture"
        row["scenario_id"] = "complete_forward_action_timeline"
        rows.append(row)
        day += timedelta(days=1)
    if change_last_units:
        rows[-1]["ordered_units"] = str(int(rows[-1]["ordered_units"]) + 1)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def forward_timeline_case(client, output, pack, identities, as_of):
    """Run a real forward business-clock sequence in its own isolated shop."""
    case = identities["forward_action_case"]
    state_path = output / "forward-action-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "run_id": output.parent.name, "pack": "05", "shop_id": case["shop_id"], "files": {}, "sources": {},
    }
    original_sales = pack / "import_ready/sales_traffic_daily_v1.csv"
    generated = output / "forward-action-inputs"
    source_template = Path(str(original_sales) + ".meta.json")
    generated.mkdir(parents=True, exist_ok=True)
    source_name = f"Forward action {state['run_id']} sales_traffic"

    baseline_path = generated / "baseline-before-action.csv"
    baseline_rows = _write_sales_slice(original_sales, baseline_path, start="2026-08-26", end="2026-09-22")
    _write_sales_metadata(baseline_path, case["shop_id"], source_name, "2026-08-26", "2026-09-22", "2026-09-23T00:00:00Z", source_template)
    assert len(baseline_rows) == 28

    observation_path = generated / "observation-after-action.csv"
    observation_rows = _write_sales_slice(original_sales, observation_path, start="2026-09-24", end="2026-09-30")
    _write_sales_metadata(observation_path, case["shop_id"], source_name, "2026-09-24", "2026-09-30", "2026-10-01T20:00:00Z", source_template)
    assert len(observation_rows) == 7

    revision_path = generated / "observation-correction-after-review.csv"
    revision_rows = _write_sales_slice(original_sales, revision_path, start="2026-09-24", end="2026-09-30", change_last_units=True)
    _write_sales_metadata(revision_path, case["shop_id"], source_name, "2026-09-24", "2026-09-30", "2026-10-01T20:00:00Z", source_template)
    assert len(revision_rows) == 7
    _set_business_clock(output, "2026-09-22", "forward-action source freeze before action effective date")
    baseline_batch = import_file(client, baseline_path, case["shop_id"], state, state_path, source_key="forward_sales")
    assert baseline_batch["valid_rows"] == 28
    scan = queued(client, "/api/v1/snapshots/scan/", {
        "shop": case["shop_id"], "child_asin": "D5X2600005", "idempotency_key": state["run_id"] + "-forward-baseline-scan"
    })
    baseline_snapshot = checked(client, "GET", f"/api/v1/snapshots/{scan['result']['snapshot_id']}/")
    frozen = artifacts(client, case["shop_id"], "D5X2600005", state["run_id"] + "-forward-before-action")
    report, investigation = frozen["report"], frozen["investigation"]
    assert investigation["status"] in {"completed", "partial"}
    assert report["metric_snapshot"] == investigation["input_versions"]["metric_snapshot"] == baseline_snapshot["id"]
    assert report["input_versions"]["source_manifest"] == investigation["input_versions"]["source_manifest"]
    assert report["input_versions"]["mapping_versions"] == investigation["input_versions"]["mapping_versions"]
    steps = investigation.get("steps", [])
    assert steps, "completed investigation must expose its persisted step clock"
    investigation_completed_at = max((step["created_at"] for step in steps), key=lambda value: value)

    title = f"[SYNTHETIC] forward action {state['run_id']} D5X2600005"
    task_rows = checked(client, "GET", "/api/v1/action-tasks/", params={"shop": case["shop_id"], "page_size": 100})["results"]
    task = next((item for item in task_rows if item["title"] == title), None)
    if task is None:
        members = checked(client, "GET", f"/api/v1/shops/{case['shop_id']}/members/")
        actor_id = next(item["id"] for item in members["items"] if item["username"] == "accept_admin")
        task = checked(client, "POST", "/api/v1/action-tasks/", json={
            "shop": case["shop_id"], "child_asin": "D5X2600005", "title": title, "assigned_to": actor_id,
            "due_at": "2026-09-24T13:00:00-07:00", "question": "完整观察窗口是否变化？",
            "completion_criteria": "保留完整日窗和原始证据，不宣称因果", "observed_metrics": ["ordered_units"],
            "is_formal": True, "source_report": report["id"], "source_investigation": investigation["id"],
            "confirm_source": True, "confirmation_note": "人工确认采用动作生效前冻结的报告、调查和指标快照。",
            "evidence_refs": [f"report:{report['id']}", f"investigation:{investigation['id']}", f"snapshot:{baseline_snapshot['id']}"],
        })
    state.update(source_report=report, source_investigation=investigation, baseline_snapshot=baseline_snapshot, task=task)
    save(state_path, state)

    _set_business_clock(output, "2026-09-23", "action became effective after source review and human confirmation")
    task = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")
    if task["status"] != "completed":
        task = checked(client, "POST", f"/api/v1/action-tasks/{task['id']}/complete/", json={
            "expected_version": task["version"], "notes": "按已确认计划执行合成动作；仅记录复查窗口，不作因果结论。",
            "effective_at": "2026-09-23T12:00:00-07:00", "review_at": "2026-10-01T13:00:00-07:00",
            "observed_metrics": ["ordered_units"],
            "evidence_refs": [f"report:{report['id']}", f"investigation:{investigation['id']}", f"snapshot:{baseline_snapshot['id']}"],
        })
    state["task"] = task
    save(state_path, state)

    _set_business_clock(output, "2026-10-01", "complete the seven post-action business dates before review")
    observation_batch = import_file(client, observation_path, case["shop_id"], state, state_path, source_key="forward_sales")
    assert observation_batch["valid_rows"] == 7
    scan = queued(client, "/api/v1/snapshots/scan/", {
        "shop": case["shop_id"], "child_asin": "D5X2600005", "idempotency_key": state["run_id"] + "-forward-review-scan-v1"
    })
    review_snapshot = checked(client, "GET", f"/api/v1/snapshots/{scan['result']['snapshot_id']}/")
    task = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")
    if task["review_status"] != "completed":
        task = checked(client, "POST", f"/api/v1/action-tasks/{task['id']}/review/", json={
            "expected_version": task["version"], "status": "completed", "result": "完整窗口已核对；观察到变化，不据此认定动作造成变化。",
            "review_at": "2026-10-01T13:00:00-07:00", "snapshot": review_snapshot["id"],
        })
    comparison = task["review_comparison"]["ordered_units"]
    oracle = action_oracle(read_rows(original_sales), "D5X2600005", "2026-09-23")
    for field in ("baseline_total", "current_total", "baseline", "current", "delta"):
        assert Decimal(str(comparison[field])) == Decimal(str(oracle[field])), (field, comparison[field], oracle[field])
    assert comparison["window_start"] == "2026-09-24" and comparison["window_end"] == "2026-09-30"
    assert comparison["baseline_days"] == 28 and comparison["current_days"] == 7
    frozen_report_payload = report["payload"]
    frozen_inv_result = investigation["result"]
    frozen_lineage = task["source_lineage"]
    frozen_comparison = task["review_comparison"]

    revision_batch = import_file(client, revision_path, case["shop_id"], state, state_path, source_key="forward_sales")
    assert revision_batch["valid_rows"] == 7
    scan = queued(client, "/api/v1/snapshots/scan/", {
        "shop": case["shop_id"], "child_asin": "D5X2600005", "idempotency_key": state["run_id"] + "-forward-review-scan-v2"
    })
    revised_snapshot = checked(client, "GET", f"/api/v1/snapshots/{scan['result']['snapshot_id']}/")
    report_after = checked(client, "GET", f"/api/v1/reports/{report['id']}/")
    investigation_after = checked(client, "GET", f"/api/v1/investigations/{investigation['id']}/")
    task_after = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")
    assert report_after["payload"] == frozen_report_payload
    assert investigation_after["result"] == frozen_inv_result
    assert task_after["source_lineage"] == frozen_lineage
    assert task_after["review_comparison"] == frozen_comparison
    assert revised_snapshot["source_manifest"] != review_snapshot["source_manifest"]

    timeline = {
        "schema_version": "forward-action-timeline-v1", "status": "passed", "source_nature": "synthetic_acceptance_fixture",
        "shop_id": case["shop_id"], "asin": "D5X2600005", "business_as_of": as_of,
        "report_created_at": report["created_at"], "investigation_completed_at": investigation_completed_at,
        "human_confirmed_at": task["human_confirmed_at"], "task_created_at": task["created_at"],
        "effective_at": task["effective_at"], "observation_start": comparison["window_start"],
        "observation_end": comparison["window_end"], "review_at": task["review_at"],
        "report_id": report["id"], "investigation_id": investigation["id"], "task_id": task["id"],
        "task_title": task["title"], "human_confirmation_note": task["human_confirmation_note"],
        "source_report_manifest": report["input_versions"]["source_manifest"],
        "source_investigation_manifest": investigation["input_versions"]["source_manifest"],
        "review_manifest": review_snapshot["source_manifest"], "post_review_revision_manifest": revised_snapshot["source_manifest"],
        "mapping_versions": report["input_versions"]["mapping_versions"], "baseline_snapshot_id": baseline_snapshot["id"],
        "review_snapshot_id": review_snapshot["id"], "revision_snapshot_id": revised_snapshot["id"],
        "comparison": comparison, "old_report_investigation_task_and_review_unchanged_after_revision": True,
        "causal_or_revenue_claim": False,
    }
    timeline_order = [timeline[key] for key in ("report_created_at", "investigation_completed_at", "human_confirmed_at", "task_created_at", "effective_at")]
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in timeline_order]
    assert all(left <= right for left, right in zip(parsed, parsed[1:])), timeline_order
    assert timeline["effective_at"] < timeline["observation_start"] <= timeline["observation_end"] < timeline["review_at"]
    save(output / "representative-lineage.json", timeline)
    save(state_path, {**state, "timeline": timeline, "task": task_after})


def forward_timeline_complete_case(client, output, pack, identities):
    """Run a separate forward closure with a complete pre-action daily baseline."""
    case = identities["forward_complete_action_case"]
    state_path = output / "forward-complete-v2-action-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "pack": "05",
        "run_id": output.parent.name,
        "scenario_id": "complete_daily_baseline_forward_action_v2",
        "shop_id": case["shop_id"],
        "asin": case["child_asin"],
        "files": {},
        "sources": {},
    }
    original_sales = pack / "import_ready/sales_traffic_daily_v1.csv"
    generated = output / "forward-complete-action-inputs-v2"
    source_template = Path(str(original_sales) + ".meta.json")
    generated.mkdir(parents=True, exist_ok=True)
    source_name = f"Forward complete action {state['run_id']} sales_traffic"
    asin = case["child_asin"]

    baseline_path = generated / "complete-baseline-before-action.csv"
    baseline_rows = _write_complete_forward_sales_slice(
        original_sales,
        baseline_path,
        start="2026-08-20",
        end="2026-09-23",
        target_asin=asin,
    )
    _write_sales_metadata(
        baseline_path,
        case["shop_id"],
        source_name,
        "2026-08-20",
        "2026-09-23",
        "2026-09-24T12:00:00Z",
        source_template,
        asin=asin,
    )
    assert len(baseline_rows) == 35

    observation_path = generated / "complete-observation-after-action.csv"
    observation_rows = _write_complete_forward_sales_slice(
        original_sales,
        observation_path,
        start="2026-09-25",
        end="2026-10-01",
        target_asin=asin,
    )
    _write_sales_metadata(
        observation_path,
        case["shop_id"],
        source_name,
        "2026-09-25",
        "2026-10-01",
        "2026-10-02T12:00:00Z",
        source_template,
        asin=asin,
    )
    assert len(observation_rows) == 7

    correction_path = generated / "complete-observation-correction-after-review.csv"
    correction_rows = _write_complete_forward_sales_slice(
        original_sales,
        correction_path,
        start="2026-09-25",
        end="2026-10-01",
        target_asin=asin,
        change_last_units=True,
    )
    _write_sales_metadata(
        correction_path,
        case["shop_id"],
        source_name,
        "2026-09-25",
        "2026-10-01",
        "2026-10-03T12:00:00Z",
        source_template,
        asin=asin,
    )
    assert len(correction_rows) == 7

    _set_business_clock(output, "2026-09-24", "complete baseline through prior day before simulated action")
    baseline_batch = import_file(
        client, baseline_path, case["shop_id"], state, state_path, source_key="forward_complete_sales"
    )
    assert baseline_batch["valid_rows"] == 35
    baseline_scan = queued(client, "/api/v1/snapshots/scan/", {
        "shop": case["shop_id"],
        "child_asin": asin,
        "idempotency_key": state["run_id"] + "-forward-complete-v2-baseline-scan",
    })
    baseline_snapshot = checked(client, "GET", f"/api/v1/snapshots/{baseline_scan['result']['snapshot_id']}/")
    daily = baseline_snapshot["payload"]["daily"]
    assert daily["status"] == "ready", daily
    assert daily["baseline"]["complete"] is True and daily["current"]["complete"] is True, daily
    assert daily["baseline"]["start"] == "2026-08-20" and daily["current"]["end"] == "2026-09-23"

    frozen = artifacts(
        client,
        case["shop_id"],
        asin,
        state["run_id"] + "-forward-complete-v2-daily-metrics",
        investigation_query="只核对冻结的销量与流量日指标的基线窗、当前窗、完整状态与来源；不推断原因。",
    )
    report, investigation = frozen["report"], frozen["investigation"]
    assert investigation["status"] == "completed", investigation
    assert report["metric_snapshot"] == investigation["input_versions"]["metric_snapshot"] == baseline_snapshot["id"]
    assert report["input_versions"]["source_manifest"] == investigation["input_versions"]["source_manifest"]
    run_evidence = (investigation.get("result") or {}).get("evidence", [])
    investigation_snapshot = next(
        (item for item in run_evidence if item.get("type") == "metric_snapshot"), {}
    )
    investigation_daily = (investigation_snapshot.get("metrics") or {}).get("daily") or {}
    assert investigation_daily.get("status") == "ready", investigation_daily
    workplan = (investigation.get("result") or {}).get("business_basis") or {}
    subtasks = workplan.get("subtasks") or []
    assert [(item.get("family"), item.get("mode"), item.get("status")) for item in subtasks] == [
        ("daily", "daily", "facts_available")
    ], workplan
    assert investigation["result"].get("completion_status") == "facts_available"

    members = checked(client, "GET", f"/api/v1/shops/{case['shop_id']}/members/")
    actor_id = next(item["id"] for item in members["items"] if item["username"] == "accept_admin")
    title = f"[SYNTHETIC] complete forward action {state['run_id']} {asin} daily metrics v2"
    task = checked(client, "POST", "/api/v1/action-tasks/", json={
        "shop": case["shop_id"],
        "child_asin": asin,
        "title": title,
        "assigned_to": actor_id,
        "due_at": "2026-09-26T13:00:00-07:00",
        "question": "完整的动作后七日窗口是否变化？",
        "completion_criteria": "核对完整来源窗口与旧版本 lineage，不作因果结论",
        "observed_metrics": ["ordered_units"],
        "is_formal": True,
        "source_report": report["id"],
        "source_investigation": investigation["id"],
        "confirm_source": True,
        "confirmation_note": "模拟人工确认采用动作生效前完整且已冻结的报告、调查和日指标快照。",
        "evidence_refs": [
            f"report:{report['id']}",
            f"investigation:{investigation['id']}",
            f"snapshot:{baseline_snapshot['id']}",
        ],
    })
    assert task["human_confirmed_by"] and task["human_confirmed_at"]
    task = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")

    _set_business_clock(output, "2026-09-25", "advance past action effective time before recording completion")
    task = checked(client, "POST", f"/api/v1/action-tasks/{task['id']}/complete/", json={
        "expected_version": task["version"],
        "notes": "按模拟人工确认的计划完成动作，仅观察之后完整日窗。",
        "effective_at": "2026-09-24T14:00:00-07:00",
        "review_at": "2026-10-02T13:00:00-07:00",
        "observed_metrics": ["ordered_units"],
        "evidence_refs": [
            f"report:{report['id']}",
            f"investigation:{investigation['id']}",
            f"snapshot:{baseline_snapshot['id']}",
        ],
    })
    state.update(
        report=report,
        investigation=investigation,
        baseline_snapshot=baseline_snapshot,
        baseline_batch=baseline_batch,
        task=task,
    )
    save(state_path, state)

    _set_business_clock(output, "2026-10-02", "seven complete post-action dates ended before review")
    _relogin_acceptance_admin(client, output)
    observation_batch = import_file(
        client, observation_path, case["shop_id"], state, state_path, source_key="forward_complete_sales"
    )
    assert observation_batch["valid_rows"] == 7
    review_scan = queued(client, "/api/v1/snapshots/scan/", {
        "shop": case["shop_id"],
        "child_asin": asin,
        "idempotency_key": state["run_id"] + "-forward-complete-v2-review-scan-v1",
    })
    review_snapshot = checked(client, "GET", f"/api/v1/snapshots/{review_scan['result']['snapshot_id']}/")
    task = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")
    if task["review_status"] != "completed":
        task = checked(client, "POST", f"/api/v1/action-tasks/{task['id']}/review/", json={
            "expected_version": task["version"],
            "status": "completed",
            "result": "完整的七日观察窗口已核对；变化不构成动作因果或收益证明。",
            "review_at": "2026-10-02T13:00:00-07:00",
            "snapshot": review_snapshot["id"],
        })
    comparison = task["review_comparison"]["ordered_units"]
    oracle = action_oracle([*baseline_rows, *observation_rows], asin, "2026-09-24")
    for field in ("baseline_total", "current_total", "baseline", "current", "delta"):
        assert Decimal(str(comparison[field])) == Decimal(str(oracle[field])), (field, comparison[field], oracle[field])
    assert comparison["window_start"] == "2026-09-25" and comparison["window_end"] == "2026-10-01"
    assert comparison["window_evidence"]["baseline"]["complete"] is True
    assert comparison["window_evidence"]["current"]["complete"] is True
    frozen_task = task
    frozen_report_payload = report["payload"]
    frozen_investigation_result = investigation["result"]
    frozen_source_lineage = task["source_lineage"]
    frozen_comparison = task["review_comparison"]

    _set_business_clock(output, "2026-10-03", "corrected observation arrives after review")
    _relogin_acceptance_admin(client, output)
    correction_batch = import_file(
        client, correction_path, case["shop_id"], state, state_path, source_key="forward_complete_sales"
    )
    assert correction_batch["valid_rows"] == 7
    correction_scan = queued(client, "/api/v1/snapshots/scan/", {
        "shop": case["shop_id"],
        "child_asin": asin,
        "idempotency_key": state["run_id"] + "-forward-complete-v2-review-scan-v2",
    })
    correction_snapshot = checked(client, "GET", f"/api/v1/snapshots/{correction_scan['result']['snapshot_id']}/")
    report_after = checked(client, "GET", f"/api/v1/reports/{report['id']}/")
    investigation_after = checked(client, "GET", f"/api/v1/investigations/{investigation['id']}/")
    task_after = checked(client, "GET", f"/api/v1/action-tasks/{frozen_task['id']}/")
    assert report_after["payload"] == frozen_report_payload
    assert investigation_after["result"] == frozen_investigation_result
    assert task_after["source_lineage"] == frozen_source_lineage
    assert task_after["review_comparison"] == frozen_comparison
    before_revision = review_snapshot["source_manifest"]
    after_revision = correction_snapshot["source_manifest"]
    assert before_revision != after_revision

    timeline_order = [
        report["created_at"],
        max((step["created_at"] for step in investigation.get("steps", [])), default=investigation["created_at"]),
        task["human_confirmed_at"],
        task["effective_at"],
    ]
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in timeline_order]
    assert all(left <= right for left, right in zip(parsed, parsed[1:])), timeline_order
    assert task["effective_at"] < comparison["window_start"] <= comparison["window_end"] < task["review_at"]
    lineage = {
        "schema_version": "forward-action-complete-lineage-v1",
        "scenario_id": "complete_daily_baseline_forward_action",
        "status": "passed",
        "source_nature": "synthetic_acceptance_fixture",
        "confirmation_nature": "simulated_acceptance_actor_confirmation",
        "shop_id": case["shop_id"],
        "asin": asin,
        "report_id": report["id"],
        "report_status": report["payload"].get("status"),
        "investigation_id": investigation["id"],
        "investigation_status": investigation["status"],
        "investigation_completion_status": investigation["result"].get("completion_status"),
        "requested_metric_subtasks": subtasks,
        "report_claim_scope": "actual report status is recorded; this acceptance establishes only the requested sales/traffic daily metric workplan, not full business-domain report completeness",
        "investigation_stop_reason": investigation.get("stop_reason", ""),
        "task_id": task["id"],
        "task_title": task["title"],
        "human_confirmed_at": task["human_confirmed_at"],
        "human_confirmation_note": task["human_confirmation_note"],
        "baseline_snapshot_id": baseline_snapshot["id"],
        "report_metric_snapshot_id": report["metric_snapshot"],
        "investigation_metric_snapshot_id": investigation["input_versions"]["metric_snapshot"],
        "pre_action_cutoff": daily["cutoff"],
        "pre_action_daily_status": daily["status"],
        "pre_action_baseline_complete": daily["baseline"]["complete"],
        "pre_action_current_complete": daily["current"]["complete"],
        "pre_action_daily_baseline_window": {"start": daily["baseline"]["start"], "end": daily["baseline"]["end"]},
        "pre_action_daily_current_window": {"start": daily["current"]["start"], "end": daily["current"]["end"]},
        "pre_action_rows": len(baseline_rows),
        "effective_at": task["effective_at"],
        "observation_window": {"start": comparison["window_start"], "end": comparison["window_end"]},
        "review_at": task["review_at"],
        "observation_rows": len(observation_rows),
        "review_snapshot_id": review_snapshot["id"],
        "correction_snapshot_id": correction_snapshot["id"],
        "review_comparison": comparison,
        "old_report_investigation_task_and_review_unchanged_after_correction": True,
        "corrected_source_manifest_differs": True,
        "causal_or_revenue_claim": False,
        "limitation": "Synthetic fixture with simulated operator confirmation; demonstrates source completeness, lineage and review mechanics only, not action causality or business uplift.",
    }
    save(output / "representative-lineage-complete.json", lineage)
    save(state_path, {
        **state,
        "observation_batch": observation_batch,
        "correction_batch": correction_batch,
        "review_snapshot": review_snapshot,
        "correction_snapshot": correction_snapshot,
        "lineage": lineage,
        "task_after_correction": task_after,
    })
    save(output / "forward-complete-result.json", {
        "schema_version": "forward-action-complete-result-v1",
        "status": "passed",
        "scenario_id": lineage["scenario_id"],
        "report_status": lineage["report_status"],
        "report_claim_scope": "actual status recorded; this acceptance establishes only the requested sales/traffic daily metric workplan, not full business-domain report completeness",
        "investigation_status": lineage["investigation_status"],
        "pre_action_daily_status": daily["status"],
        "pre_action_rows": len(baseline_rows),
        "observation_rows": len(observation_rows),
        "causal_or_revenue_claim": False,
        "source_nature": lineage["source_nature"],
        "confirmation_nature": lineage["confirmation_nature"],
        "actual_execution_utc": datetime.now(UTC).isoformat(),
    })


def actions(client, env, output, pack, identities, as_of):
    main = json.loads((output / "state.json").read_text())
    path = output / "actions-state.json"
    state = json.loads(path.read_text()) if path.exists() else {"pack": "05", "run_id": main["run_id"], "sources": main["sources"].copy(), "files": {}, "actions": {}, "scenarios": []}
    shop = identities["shop_id"]
    manifest = json.loads((pack / "manifest.json").read_text())
    forward_timeline_case(client, output, pack, identities, as_of)
    import_file(client, pack / manifest["files"]["gap_fixture"]["path"], shop, state, path)
    scenario_path = pack.parent / "06_S5动作效果场景_v1/scenario_catalog.csv"
    if not scenario_path.is_file():
        raise ValueError("Sibling 06 scenario catalog required; do not mix dates from another run")
    assert manifest["as_of"] == as_of, "action replay date must match the selected pack"
    scenarios = read_rows(scenario_path)
    review_stamp = datetime.combine(date.fromisoformat(as_of), datetime.min.time(), ZoneInfo("America/Los_Angeles")).replace(hour=13).astimezone(UTC).isoformat()
    due_stamp = datetime.combine(date.fromisoformat(as_of)+timedelta(days=2), datetime.min.time(), ZoneInfo("America/Los_Angeles")).replace(hour=13).astimezone(UTC).isoformat()
    sales = read_rows(pack / manifest["files"]["sales_traffic"]["path"])
    members = checked(client, "GET", f"/api/v1/shops/{shop}/members/")
    user_id = next(u["id"] for u in members["items"] if u["username"] == "accept_admin")
    for scenario in scenarios:
        sid, asin = scenario["scenario_id"], scenario["asin"]
        if sid in {r["scenario_id"] for r in state["scenarios"]}:
            continue
        job = queued(client, "/api/v1/snapshots/scan/", {"shop": shop, "child_asin": asin, "idempotency_key": f"{state['run_id']}-{sid}-action-scan-{len(state['actions'])}-v3"})
        snapshot = job["result"]["snapshot_id"]
        event = None
        if sid == "closed_then_recurrence":
            events = checked(client, "GET", "/api/v1/events/", params={"shop": shop, "child_asin": asin, "page_size": 100})
            event = next(e for e in events["results"] if e["child_asin"] == asin and e["family"] == "sales")
        if sid not in state["actions"]:
            payload = {"shop": shop, "child_asin": asin, **({"event": event["id"]} if event else {}),
                "title": f"[SYNTHETIC] release {sid}", "assigned_to": user_id, "due_at": due_stamp, "question": "完整观察窗口是否变化？", "completion_criteria": "比较源事实，不宣称因果", "observed_metrics": ["ordered_units"], "evidence_refs": [f"snapshot:{snapshot}"], "is_formal": True}
            created = checked(client, "POST", "/api/v1/action-tasks/", json=payload)
            state["actions"][sid] = created
            save(path, state)
        task = checked(client, "GET", f"/api/v1/action-tasks/{state['actions'][sid]['id']}/")
        if task["status"] != "completed":
            task = checked(client, "POST", f"/api/v1/action-tasks/{task['id']}/complete/", json={"expected_version": task["version"], "notes": "真实API回放合成动作", "effective_at": datetime.combine(date.fromisoformat(scenario["effective_date"]), datetime.min.time(), ZoneInfo("America/Los_Angeles")).replace(hour=12).isoformat(), "review_at": review_stamp, "observed_metrics": ["ordered_units"], "evidence_refs": [f"snapshot:{snapshot}"]})
        response = client.get(f"/api/v1/action-tasks/{task['id']}/") if task.get("review_status") == "completed" else client.post(f"/api/v1/action-tasks/{task['id']}/review/", json={"expected_version": task["version"], "status": "completed", "result": "仅核查窗口变化，不归因。", "review_at": review_stamp, "snapshot": snapshot})
        expected = None
        if scenario["expected_outcome"] == "not_reviewable":
            assert response.status_code == 400, response.text
            fragment = "完整站点日" if sid == "window_not_mature" else "缺报日期"
            assert fragment in response.text
            saved = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")
            assert saved["review_status"] == "scheduled" and saved["review_comparison"] == {}
        else:
            assert response.status_code == 200, response.text
            comparison = response.json()["review_comparison"]["ordered_units"]
            expected = action_oracle(sales, asin, scenario["effective_date"])
            for field in ["baseline_total", "current_total", "baseline", "current", "delta"]:
                assert Decimal(comparison[field]) == Decimal(expected[field]), (sid, field)
            assert comparison["basis"] == "daily_mean" and comparison["unit"] == "units/day"
            assert comparison["baseline_days"] == 28 and comparison["current_days"] == 7
            assert comparison["window_start"] == expected["current_start"] and comparison["window_end"] == expected["current_end"]
            assert len(comparison["window_evidence"]["baseline"]["raw_row_ids"]) == 28 and len(comparison["window_evidence"]["current"]["raw_row_ids"]) == 7
        result = {"scenario_id": sid, "asin": asin, "task_id": task["id"], "snapshot_id": snapshot, "expected_outcome": scenario["expected_outcome"], "oracle": expected, "response_status": response.status_code, "actual": response.json()}
        if event:
            event = checked(client, "GET", f"/api/v1/events/{event['id']}/")
            if event["status"] != "closed":
                if event["status"] == "pending":
                    event = checked(client, "POST", f"/api/v1/events/{event['id']}/command/", json={"expected_version": event["version"], "operation": "claim", "reason": "隔离验收领取"})
                event = checked(client, "POST", f"/api/v1/events/{event['id']}/command/", json={"expected_version": event["version"], "operation": "observe", "action_task": task["id"], "reason": "完整观察已复查"})
                event = checked(client, "POST", f"/api/v1/events/{event['id']}/command/", json={"expected_version": event["version"], "operation": "close", "closure_reason": "known_change", "reason": "已核查合成已知窗口，保留原因边界", "evidence_refs": [snapshot]})
            assert event["status"] == "closed"
            save(output / "before-recurrence-closed-event.json", event)
            original_task = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")
            save(output / "business-clock.json", {"as_of": str(date.fromisoformat(as_of)+timedelta(days=7)), "reason": "explicit synthetic recurrence replay after closure", "changed_at_real_utc": datetime.now(UTC).isoformat()})
            session = checked(client, "GET", "/api/v1/session/")
            account = next(a for a in json.loads((output / "accounts.json").read_text()) if a["username"] == "accept_admin")
            login = checked(client, "POST", "/api/v1/login/", json=account, headers={"X-CSRFToken": session["csrf_token"]})
            client.headers["X-CSRFToken"] = login["csrf_token"]
            for info in manifest["files"]["recurrence_fixture"]:
                import_file(client, pack / info["path"], shop, state, path)
            recurrence = queued(client, "/api/v1/snapshots/scan/", {"shop": shop, "child_asin": asin, "idempotency_key": state["run_id"]+"-after-closure-recurrence"})
            reopened = checked(client, "GET", f"/api/v1/events/{event['id']}/")
            assert reopened["status"] == "pending"
            kept = checked(client, "GET", f"/api/v1/action-tasks/{task['id']}/")
            assert kept["review_comparison"] == original_task["review_comparison"]
            save(output / "after-closure-recurrence.json", {"closed_event": event, "reopened_event": reopened, "snapshot": recurrence["result"], "old_action_preserved": True, "rows_imported_after_closure": sum(i["rows"] for i in manifest["files"]["recurrence_fixture"])})
        state["scenarios"].append(result)
        save(path, state)
    # Produce an actual report/investigation from the fully imported shop, not another seed.
    state["artifacts"] = artifacts(client, shop, "D5X2600005", state["run_id"]+"-fully-imported-05")
    save(path, state)
    save(output / "actions-result.json", {"status": "passed", "scenario_count": len(state["scenarios"]), "gap_rows": 119, "recurrence_rows": 21,
                                          "source_nature": "synthetic_acceptance_fixture", "independent_oracle": "CSV Decimal and exact source dates; expected_outcome not used by runtime", "actual_execution_utc": datetime.now(UTC).isoformat()})


def run_followups(phase, client, env, output, pack, identities, as_of):
    if phase == "lifecycle":
        return lifecycle(client, env, output, pack, identities, as_of)
    if phase == "originals":
        return originals(client, env, output, pack, identities, as_of)
    if phase == "actions":
        return actions(client, env, output, pack, identities, as_of)
    if phase == "forward-complete":
        return forward_timeline_complete_case(client, output, pack, identities)
    return browser(client, env, output, pack, identities, as_of)


def browser(client, env, output, pack, identities, as_of):
    import socket
    port = 5186 if identities["pack"] == "04" else 5188
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError("Browser port occupied; refusing reuse")
    history = output / "browser-history" / str(time.time_ns())
    history.mkdir(parents=True)
    if (output / "browser-result.json").exists():
        shutil.copy2(output / "browser-result.json", history / "previous-result.json")
    if (output / "screenshots").exists():
        shutil.copytree(output / "screenshots", history / "previous-screenshots")
    save(output / "browser-result.json", {"status": "not_run", "started_utc": datetime.now(UTC).isoformat(), "attempt_history": str(history)})
    screenshots = output / "screenshots"
    screenshots.mkdir(exist_ok=True)
    browser_env = {**env, "ACCEPTANCE_API_PROXY": str(client.base_url).rstrip("/"), "ACCEPTANCE_BROWSER_BASE_URL": f"http://127.0.0.1:{port}",
                   "ACCEPTANCE_ACCOUNTS_FILE": str(output / "accounts.json"), "RELEASE_PACKAGE_OUTPUT": str(output)}
    handle = (output / "browser-web.log").open("a")
    process = subprocess.Popen(["pnpm", "dev", "--port", str(port)], cwd=ROOT / "frontend", env=browser_env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        import httpx
        for _ in range(120):
            try:
                if httpx.get(browser_env["ACCEPTANCE_BROWSER_BASE_URL"], timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        with (output / "browser.log").open("a") as log:
            result = subprocess.run(["pnpm", "exec", "playwright", "test", "release_package.spec.ts", "--reporter=list"], cwd=ROOT / "frontend", env=browser_env, stdout=log, stderr=subprocess.STDOUT, check=False)
        assert result.returncode == 0, "Imported-package browser acceptance failed; inspect browser.log"
        save(output / "browser-result.json", {"status": "passed", "pack": identities["pack"], "shop_id": identities["shop_id"], "widths": [390, 768, 1440], "screenshots": len(list(screenshots.glob("*.png"))), "actual_utc": datetime.now(UTC).isoformat()})
    except Exception as exc:
        save(output / "browser-result.json", {"status": "failed", "pack": identities["pack"], "error_type": type(exc).__name__, "actual_utc": datetime.now(UTC).isoformat(), "attempt_history": str(history)})
        raise
    finally:
        shutil.copy2(output / "browser-result.json", history / "attempt-result.json")
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        handle.close()


def originals(client, env, output, pack, identities, as_of):
    import csv
    import hashlib
    original = ROOT / "dataset/data/01_标准化评论数据/评论数据集_v1.csv"
    allowed_asins = sorted({p["child_asin"] for p in identities["products"]})
    selected = {asin: [] for asin in allowed_asins}
    seen = set()
    lines = []
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    with original.open(encoding="utf-8-sig", newline="") as handle:
        for line, row in enumerate(csv.DictReader(handle), 2):
            asin = row["sku"]
            # A documented controlled range, not all 81,277 comments or a model gold lookup.
            if asin not in selected or len(selected[asin]) >= 200 or row["review_id"] in seen:
                continue
            seen.add(row["review_id"])
            instant = datetime.fromisoformat(row["created_at"])
            selected[asin].append({"child_asin": asin, "seller_sku": "", "fulfillment": "", "business_date": str(instant.astimezone(ZoneInfo("America/Los_Angeles")).date()),
                                   "record_id": row["review_id"], "rating": str(int(Decimal(row["rating"]))), "text": row["text"] or row["title"], "title": row["title"], "collected_at": instant.isoformat(), "language": "und", "verified_purchase": row["verified_purchase"], "is_vine": "", "is_deleted": "false"})
            lines.append({"source_line": line, "record_id": row["review_id"], "asin": asin, "language_review": "original language retained; no language model classification"})
    rows = [r for asin in allowed_asins for r in selected[asin]]
    assert len(rows) == 600 and len(seen) == 600
    target = output / "controlled-original-reviews.csv"
    with target.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    target.chmod(0o600)
    dates = sorted(r["business_date"] for r in rows)
    meta = {"source_payload": {"shop": identities["shop_id"], "name": "original read-only controlled subset", "kind": "reviews", "is_primary": True, "grain": "asin_record",
                               "column_mapping": {k: k for k in rows[0]}, "ignored_columns": [], "coverage_basis": "business_date", "empty_report_means_zero": False},
            "upload_payload": {"coverage_start": dates[0], "coverage_end": dates[-1], "generated_at": "2026-10-07T00:00:00Z", "declared_complete": False, "declared_asins": allowed_asins}}
    save(Path(str(target)+".meta.json"), meta)
    state_path = output / "originals-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"pack": "01", "run_id": output.parent.name, "sources": {}, "files": {}}
    batch = import_file(client, target, identities["shop_id"], state, state_path, source_key="original-reviews")
    assert batch["valid_rows"] == 600 and batch["error_rows"] == 0
    save(output / "originals-lineage.json", {"source_path": str(original.relative_to(ROOT)), "source_sha256": original_hash,
                                            "selection": "first 200 unique review_id in source order for each of the three 04 ASINs; no gold/qrels used", "rows": 600, "original_row_references": lines, "declared_complete": False})
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_hash
    sampled = rows[0]
    evidence = checked(client, "POST", "/api/v1/evidence-search/", json={"shop": identities["shop_id"], "child_asin": sampled["child_asin"], "query": next(word for word in sampled["text"].split() if word.isascii() and word.isalpha() and len(word) >= 4), "start": dates[0], "end": dates[-1], "kind": "reviews"})
    save(output / "originals-retrieval.json", evidence)
    assert evidence["match_status"] == "relevant_hits" and evidence["relevant_hit_count"] > 0, evidence
    assert evidence["scope"]["asin"] == sampled["child_asin"]
    selected_by_id = {r["record_id"]: r for r in rows if r["child_asin"] == sampled["child_asin"]}
    assert all(item["record_id"] in selected_by_id and item["text"] == selected_by_id[item["record_id"]]["text"] and item["raw_row_id"] and item["fact_id"] for item in evidence["items"])
    save(output / "originals-result.json", {"status": "passed", "scope": "controlled 600 original reviews, real worker/API, 600 unique IDs and original line/file trace; no full-corpus coverage claim", "batch_id": batch["id"], "source_unchanged": True, "external_transmission": False})
