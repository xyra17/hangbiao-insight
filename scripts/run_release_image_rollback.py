"""Read-only local application rollback across two digests on an owned DB clone."""
import argparse
import hashlib
import json
import os
import socket
import subprocess
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


def persisted_detail(kind, payload):
    """Separate declared live capability metadata from persisted business fields."""
    payload = dict(payload)
    capabilities = payload.pop("tool_permissions", None) if kind == "investigations" else None
    return payload, capabilities


def validate_capability_delta(current, previous):
    if current == previous:
        return "unchanged"
    added = {"tool_name": "order_records", "mode": "read", "roles": ["data_admin"],
             "description": "读取授权冻结订单、退款和退货样本", "external_write": False}
    if not isinstance(current, list) or not isinstance(previous, list) or current.count(added) != 1 or [item for item in current if item != added] != previous:
        raise AssertionError("unreviewed investigation capability metadata change")
    return "reviewed_readonly_order_tool_addition"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ownership-file", type=Path, required=True)
    parser.add_argument("--credentials-file", type=Path, required=True)
    parser.add_argument("--current-image", required=True)
    parser.add_argument("--previous-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--current-semantic-effort", choices=["none", "low"])
    parser.add_argument("--current-semantic-max-output-tokens", type=int)
    args = parser.parse_args()
    owner_file, output = args.ownership_file.resolve(), args.output.resolve()
    if not owner_file.is_relative_to(ROOT/".local/acceptance-artifacts") or not output.is_relative_to(ROOT/".local/acceptance-artifacts"):
        parser.error("explicit local release ownership required")
    owner = json.loads(owner_file.read_text())
    if not owner_file.is_relative_to(Path(owner["run_dir"])):
        parser.error("unbound owner")
    source = owner["database"]
    database = "insight_rollback_"+uuid.uuid4().hex[:12]
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    compose = ["docker", "compose", "--env-file", str(ROOT/".env.local"), "-f", str(ROOT/"compose.dev.yml"), "exec", "-T", "postgres"]
    container, created = None, False
    checks, baseline, baseline_capabilities = [], None, None
    migration_code = 'import pathlib,hashlib,json; print(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in pathlib.Path("/app/backend/insight/migrations").glob("*.py")}))'
    migrations = [json.loads(subprocess.check_output(["docker", "run", "--rm", "--network", "none", image, "python", "-c", migration_code], text=True)) for image in [args.current_image, args.previous_image]]
    assert migrations[0] == migrations[1], "migration rollback compatibility not established"
    try:
        subprocess.run(compose+["createdb", "-U", "insight", "-T", source, database], check=True, capture_output=True)
        created = True
        credentials = json.loads(args.credentials_file.read_text())
        credentials = credentials if isinstance(credentials, list) else credentials.get("accounts", [credentials])
        account = next(a for a in credentials if a["username"] == "accept_admin")
        for image in [args.current_image, args.previous_image]:
            digest = subprocess.check_output(["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
            name = "insight-local-rollback-"+uuid.uuid4().hex[:12]
            command = ["docker", "run", "--detach", "--name", name, "--publish", f"127.0.0.1:{port}:8000", "--env", "DJANGO_SECRET_KEY", "--env", "INSIGHT_DB_PASSWORD", "--env", "CELERY_BROKER_URL", "--env", "DJANGO_DEBUG=1", "--env", "DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost", "--env", "PGHOST=host.docker.internal", "--env", "PGPORT=55432", "--env", "PGUSER=insight", "--env", f"PGDATABASE={database}", "--volume", f"{ROOT / '.local/acceptance-storage' / source}:/data/storage:ro", image]
            if image == args.current_image and args.current_semantic_effort:
                command[-1:-1] = ["--env", f"SEMANTIC_REASONING_EFFORT={args.current_semantic_effort}"]
            if image == args.current_image and args.current_semantic_max_output_tokens is not None:
                if not 1 <= args.current_semantic_max_output_tokens <= 32768:
                    raise ValueError("invalid candidate output budget")
                command[-1:-1] = ["--env", f"SEMANTIC_MAX_OUTPUT_TOKENS={args.current_semantic_max_output_tokens}"]
            container = subprocess.check_output(command, env=os.environ, text=True).strip()
            config_proof = None
            if image == args.current_image and args.current_semantic_effort:
                code = 'import json; from app.semantic.factory import build_semantic_gateway; g=build_semantic_gateway(); print(json.dumps({"reasoning_effort":g.config.reasoning_effort,"max_output_tokens":g.config.max_output_tokens,"model":g.config.model,"dispatches":0}))'
                config_proof = json.loads(subprocess.check_output(["docker", "exec", "-e", "DEEPSEEK_API_KEY=offline-config-audit-placeholder", container, "python", "-c", code], text=True))
                assert config_proof["reasoning_effort"] == args.current_semantic_effort
                assert config_proof["max_output_tokens"] == args.current_semantic_max_output_tokens
            session = requests.Session()
            base = f"http://127.0.0.1:{port}/api/v1"
            for _ in range(100):
                try:
                    if session.get(base+"/health/", timeout=1).status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.2)
            token = session.get(base+"/session/", timeout=10).json()["csrf_token"]
            login = session.post(base+"/login/", json={"username": account["username"], "password": account["password"]}, headers={"X-CSRFToken": token}, timeout=10)
            assert login.status_code == 200
            current, full_http_hashes, capabilities = {}, {}, None
            for kind in ["snapshots", "reports", "investigations", "action-tasks", "events"]:
                collection = session.get(base+f"/{kind}/", timeout=10)
                assert collection.status_code == 200 and collection.json()["count"] > 0
                object_id = baseline[kind]["id"] if baseline else collection.json()["results"][0]["id"]
                detail = session.get(base+f"/{kind}/{object_id}/", timeout=10)
                assert detail.status_code == 200
                payload = detail.json()
                full_http_hashes[kind] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                business, live_capabilities = persisted_detail(kind, payload)
                if kind == "investigations":
                    capabilities = live_capabilities
                current[kind] = {"id": object_id, "payload_sha256": hashlib.sha256(json.dumps(business, sort_keys=True).encode()).hexdigest()}
            capability_delta = "baseline"
            if baseline:
                capability_delta = validate_capability_delta(baseline_capabilities, capabilities)
                assert current == baseline, "application rollback changed persisted business detail"
            else:
                baseline, baseline_capabilities = current, capabilities
            checks.append({"image": image, "digest": digest, "business_http": "passed", "objects": current, "full_http_sha256": full_http_hashes, "live_capabilities": capabilities, "capability_delta": capability_delta, "semantic_config_proof": config_proof})
            subprocess.run(["docker", "rm", "--force", container], check=True, capture_output=True)
            container = None
        (output/"result.json").write_text(json.dumps({"status": "passed", "migration_hashes_equal": True, "comparison": "all persisted business fields exact; full HTTP hashes retained separately; only declared readonly order-tool capability addition allowed", "checks": checks, "scope": "current and earlier local candidates on a disposable nonempty clone; real previous production registry digest and target rollout signoff not provided", "source_db_untouched": source, "persistent_source_storage_readonly": True}, indent=2)+"\n")
    finally:
        if container:
            subprocess.run(["docker", "rm", "--force", container], check=True, capture_output=True)
        if created:
            subprocess.run(compose+["dropdb", "-U", "insight", "--force", database], check=True, capture_output=True)
        (output/"cleanup.json").write_text(json.dumps({"owned_app_removed": True, "owned_clone_dropped": created, "shared_volumes_preserved": True})+"\n")


if __name__ == "__main__":
    main()
