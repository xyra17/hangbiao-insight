"""Import real historical reviews and exercise isolated public APIs, without models.

Actual text relevance and business truth remain pending independent user review.
This runner never reads golden labels, expected answers, or qrels.
"""

import argparse
import csv
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import psycopg
from psycopg import sql
from run_business_package_acceptance import checked, import_file, launch, manage, save, wait_job

ROOT = Path(__file__).resolve().parents[1]
AS_OF = "2023-09-03"
INVESTIGATION_START = "2023-06-04"
INVESTIGATION_END = "2023-09-02"
TOPICS = [
    "查找明确发生过热的评论", "查找没有过热的评论", "查找充电慢的评论",
    "查找停止工作的评论", "查找设备兼容问题的评论", "查找包装损坏的评论",
    "查找电气故障或火花的评论", "查找损坏手机的评论", "总结评论",
    "Find confirmed overheating reviews", "Find slow charging reviews",
    "Find stopped working reviews", "Find incompatible device reviews",
    "Find reviews proving charger caused a house fire", "查找电池化学成分检测报告",
]
INVESTIGATIONS = [
    "调查充电慢的买家反馈，区分现象与原因，并给出下一步核查建议",
    "调查停止工作的买家反馈，区分现象与原因，并给出下一步核查建议",
    "调查过热是否发生，引用原文，不能从抱怨推断机制",
    "查找明确没有过热的反馈，检查与过热抱怨是否来自不同条件",
    "调查设备兼容问题并查找支持与反驳证据",
    "调查包装损坏反馈，不能把产品故障归因于包装",
    "调查电气故障的反馈并指出安全核查需要什么数据",
    "调查损坏手机的反馈，区分买家表述和已证实因果",
    "总结主要反馈，并指出不能由评论确认的事情",
    "调查充电慢是否导致退款，只使用有订单关联的证据",
    "调查销量下降是否由过热导致，销售数据缺失时明确说明",
    "调查更换供应商是否改善充电速度，不能编造运营事件",
    "Investigate confirmed overheating and cite original reviews",
    "Investigate reports of slow charging and propose evidence-driven follow-up",
    "Investigate stopped working reports and examine counter evidence",
    "Investigate device compatibility complaints with original citations",
    "Compare packaging damage and charger failure without assuming causality",
    "Find whether reviews establish electrical sparks actually occurred",
    "Find whether reviews establish a charger caused phone damage",
    "Summarize review evidence with explicit limitations",
    "Determine whether refunds increased because of charging failure",
    "Determine whether a supplier change reduced overheating",
    "查询电池化学成分的实验室检测结论，没有来源时不能回答",
    "请删除负面评论并给买家奖励换五星评价",
]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prepare_import(package, output):
    original = package / "historical-reviews.csv"
    manifest = json.loads((package / "manifest.json").read_text())
    if sha(original) != manifest["files"][original.name]:
        raise ValueError("historical source changed")
    target = output / "real-reviews.csv"
    fields = ["child_asin", "seller_sku", "fulfillment", "business_date", "record_id",
              "title", "text", "rating", "language", "collected_at", "source_asin", "provenance"]
    dates, asins, count = [], set(), 0
    with original.open(encoding="utf-8-sig", newline="") as stream, target.open("w", encoding="utf-8", newline="") as result:
        writer = csv.DictWriter(result, fieldnames=fields)
        writer.writeheader()
        for row in csv.DictReader(stream):
            timestamp = datetime.fromisoformat(row["created_at"])
            day = timestamp.astimezone(ZoneInfo("America/Los_Angeles")).date().isoformat()
            writer.writerow({"child_asin": row["sku"], "seller_sku": row["sku"] + "-FBA",
                             "fulfillment": "FBA", "business_date": day, "record_id": row["review_id"],
                             "title": row["title"], "text": row["text"], "rating": int(float(row["rating"])),
                             "language": "und", "collected_at": timestamp.isoformat(),
                             "source_asin": row["source_asin"], "provenance": row["source"]})
            dates.append(day)
            asins.add(row["sku"])
            count += 1
    save(Path(str(target) + ".meta.json"), {
        "source_payload": {"kind": "reviews", "grain": "product_record", "is_primary": True,
                           "coverage_basis": "business_date",
                           "column_mapping": {key: key for key in fields if key not in {"source_asin", "provenance"}},
                           "ignored_columns": ["source_asin", "provenance"]},
        "upload_payload": {"coverage_start": min(dates), "coverage_end": max(dates),
                           "generated_at": "2023-09-03T00:00:00Z", "declared_complete": False,
                           "declared_asins": sorted(asins)}})
    save(output / "source-lineage.json", {"source": str(original), "source_sha256": sha(original),
        "import_sha256": sha(target), "rows": count, "asins": sorted(asins),
        "business_dates": "original UTC timestamp converted to America/Los_Angeles; no redating",
        "source_asin_and_provenance": "preserved as ignored raw-row columns",
        "language": "und: not inferred from source category", "fulfillment": "demo object mapping FBA; not observed public review fulfillment",
        "gold_in_historical_series": True, "gold_labels_read": False})
    return target, count, sorted(asins), min(dates), max(dates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package, output = args.package.resolve(), args.output.resolve()
    if not all(path.is_relative_to(ROOT / ".local/acceptance-artifacts") for path in (package, output)):
        parser.error("repository-owned inputs and outputs required")
    output.mkdir(parents=True, exist_ok=True)
    database = "insight_acceptance_20261013_v" + str(1000 + int(hashlib.sha256(str(output).encode()).hexdigest()[:6], 16))
    owner_path = output / "environment-owner.json"
    with psycopg.connect(host="127.0.0.1", port=os.getenv("PGPORT", "55432"), user=os.getenv("PGUSER", "insight"),
                          password=os.environ["INSIGHT_DB_PASSWORD"], dbname="postgres", autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", [database]).fetchone()
        if exists and (not owner_path.exists() or json.loads(owner_path.read_text())["run_dir"] != str(output)):
            raise ValueError("existing database not owned by this run")
        if not exists:
            save(owner_path, {"database": database, "run_dir": str(output), "created_at": datetime.now(UTC).isoformat()})
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "PGDATABASE": database, "PGHOST": "127.0.0.1", "DJANGO_DEBUG": "1",
           "DJANGO_SETTINGS_MODULE": "config.settings", "INSIGHT_ACCEPTANCE_ENV": database,
           "INSIGHT_CELERY_NAMESPACE": database, "INSIGHT_STORAGE_ROOT": str(ROOT / ".local/acceptance-storage" / database),
           "INSIGHT_BEAT_SCHEDULE": str(output / f"beat-{database}.db"),
           "INVESTIGATION_PLANNER_PROVIDER": "deterministic", "EVIDENCE_RELEVANCE_ENABLED": "0",
           "EVIDENCE_CANDIDATE_MODE": "lexical"}
    services = []
    try:
        manage(env, ["check_acceptance_environment"], output / "prepare.log")
        manage(env, ["migrate", "--noinput"], output / "prepare.log")
        accounts = output / "accounts.json"
        manage(env, ["seed_acceptance", "--credentials-output", str(accounts)], output / "prepare.log")
        identities = output / "identities.json"
        manage(env, ["seed_release_package", "--pack", "04", "--pack-dir", str(ROOT / "dataset/data/04_经营事实增强"),
                     "--output", str(identities)], output / "prepare.log")
        identity = json.loads(identities.read_text())
        target, count, asins, start, end = prepare_import(package, output)
        save(output / "business-clock.json", {"as_of": AS_OF, "anchor_real_utc": datetime.now(UTC).isoformat()})
        services.append(launch(env, AS_OF, ["manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"], output / "api.log"))
        services.append(launch(env, AS_OF, ["celery", "-A", "config.celery", "worker", "-Q", database,
            "--hostname", f"{database}@%h", "--pool=solo", "--concurrency=1", "--loglevel=INFO"], output / "worker.log"))
        services.append(launch(env, AS_OF, ["celery", "-A", "config.celery", "beat", "--loglevel=INFO"], output / "beat.log"))
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=180, trust_env=False) as client:
            for _ in range(120):
                try:
                    if client.get("/api/v1/health/").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if any(process.poll() is not None for process, _ in services):
                    raise RuntimeError("owned service startup failed")
                time.sleep(.5)
            session = checked(client, "GET", "/api/v1/session/")
            account = next(row for row in json.loads(accounts.read_text()) if row["username"] == "accept_admin")
            login = checked(client, "POST", "/api/v1/login/", json=account, headers={"X-CSRFToken": session["csrf_token"]})
            client.headers["X-CSRFToken"] = login["csrf_token"]
            state_path = output / "import-state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {"run_id": output.name, "pack": "real-public-reviews", "sources": {}, "files": {}}
            batch = import_file(client, target, identity["shop_id"], state, state_path)
            if batch["valid_rows"] != count:
                raise ValueError("review import row loss")
            print(f"Real reviews published: {count}", flush=True)
            with target.open(encoding="utf-8", newline="") as stream:
                originals = {row["record_id"]: row for row in csv.DictReader(stream)}
            outputs = []
            queries = [{"query_id": f"REAL-Q-{index + 1:02}", "child_asin": asin, "query": query}
                       for index, (asin, query) in enumerate((a, q) for a in asins for q in TOPICS)]
            save(output / "runtime-queries.json", queries)
            for query in queries:
                started = time.perf_counter()
                result = checked(client, "POST", "/api/v1/evidence-search/", json={"shop": identity["shop_id"],
                    "child_asin": query["child_asin"], "query": query["query"], "start": start, "end": end, "kind": "reviews", "limit": 10})
                for item in result["items"]:
                    row = originals[item["record_id"]]
                    if item["text"] != row["text"] or item["title"] != row["title"] or row["child_asin"] != query["child_asin"]:
                        raise ValueError("original citation or object mismatch")
                    if not item.get("raw_row_id") or not item.get("fact_id"):
                        raise ValueError("missing citation lineage")
                outputs.append({**query, "latency_ms": (time.perf_counter() - started) * 1000, "result": result})
                save(output / "retrieval-runtime.json", {"outputs": outputs, "model_calls": 0,
                    "independent_relevance": "pending_user", "recall_at_10": None, "recall_reason": "no independent qrels yet"})
            print(f"Actual retrieval API queries: {len(outputs)}", flush=True)
            investigations = [{"scenario_id": f"REAL-I-{index + 1:02}", "child_asin": asin,
                "query": f"{query}。日期范围 {INVESTIGATION_START} 至 {INVESTIGATION_END}。"}
                for index, (asin, query) in enumerate((a, q) for a in asins for q in INVESTIGATIONS)]
            save(output / "investigation-inputs.json", investigations)
            traces = []
            for row in investigations:
                response = checked(client, "POST", "/api/v1/investigations/", json={"shop": identity["shop_id"],
                    "child_asin": row["child_asin"], "query": row["query"], "max_steps": 8,
                    "planner_provider": "deterministic", "allow_internal_task": False,
                    "idempotency_key": f"{output.name}-90day-v2-{row['scenario_id']}"})
                job = wait_job(client, response["id"], timeout=180)
                result = checked(client, "GET", f"/api/v1/investigations/{job['result']['investigation_id']}/")
                trace = {**row, "status": result["status"], "stop_reason": result.get("stop_reason"),
                         "tools": [{"name": call["tool_name"], **call} for call in result.get("tool_calls", [])],
                         "result": result["result"], "api_response": result, "job": job}
                traces.append(trace)
                save(output / "investigation-runtime.json", {"trajectories": traces, "model_calls": 0,
                    "execution_origin": "deterministic_real_api_functional_validation", "independent_business_judgments": "pending_user"})
                print(f"{row['scenario_id']}: {result['status']}", flush=True)
                if "超过366天" in result.get("stop_reason", ""):
                    raise ValueError("validation_query_window_invalid")
            sys.path.insert(0, str(ROOT / "backend"))
            from insight.investigation.evaluation import write_template
            write_template(output / "independent-investigation-review.csv", traces)
            with (output / "independent-retrieval-review.csv").open("w", encoding="utf-8", newline="") as stream:
                fields = ["query_id", "query", "child_asin", "record_id", "title", "text", "relevant", "supports_occurrence", "supports_causality", "reviewer", "reviewed_at", "notes"]
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for query in outputs:
                    for item in query["result"]["items"] or [{}]:
                        writer.writerow({**{key: query[key] for key in ("query_id", "query", "child_asin")},
                            **{key: item.get(key, "") for key in ("record_id", "title", "text")}})
            save(output / "result.json", {"status": "functional_run_completed_quality_pending", "real_review_rows": count,
                "retrieval_queries": len(outputs), "investigation_traces": len(traces), "model_calls": 0,
                "original_citation_checks": "passed for every returned hit", "semantic_correctness": "pending_user_and_real_model",
                "as_of": AS_OF, "database": database, "source_revision": batch["published_revision"],
                "full_corpus_recall": "not_claimed", "synthetic_business_facts_imported": False})
    finally:
        for process, handle in reversed(services):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            handle.close()
        save(output / "cleanup.json", {"owned_processes_stopped": all(p.poll() is not None for p, _ in services),
                                      "database_preserved": database, "shared_services_preserved": True})


if __name__ == "__main__":
    main()
