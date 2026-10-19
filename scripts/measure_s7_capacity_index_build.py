"""Time the real semantic evidence-index rebuild in the isolated S7 capacity Compose project."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VALIDATION_ROOT = REPOSITORY_ROOT / "dataset" / "data" / "08_S7容量评测_v1" / "validation"
COMPOSE_PROJECT = "s1s7-capacity-remeasure-20261005"
COMPOSE_ARGS = [
    "docker",
    "compose",
    "--project-name",
    COMPOSE_PROJECT,
    "-f",
    "compose.v2.yml",
    "-f",
    ".local/capacity-compose-override.yml",
    "--env-file",
    ".env.local",
]


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(command)}\n{result.stderr[-3000:]}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(VALIDATION_ROOT.resolve()):
        parser.error("Index-build reports must be written under the S7 capacity validation directory.")
    import_report = args.import_report.resolve()
    imported = json.loads(import_report.read_text(encoding="utf-8"))
    if imported.get("status") != "passed" or imported.get("fact_counts", {}).get("support_cases") != 1000:
        parser.error("A passed 1,000-case support import report is required before index timing.")

    started = time.perf_counter()
    build = run(COMPOSE_ARGS + ["exec", "-T", "api", "python", "manage.py", "rebuild_evidence_index"])
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    verify_code = (
        "import django,json; django.setup(); "
        "from insight.models import Review,SupportCase; "
        "from insight.semantic.indexing import INDEX_VERSION; "
        "print(json.dumps({'index_version':INDEX_VERSION,'rows':{m.__name__:{'total':m.objects.count(),"
        "'indexed':m.objects.filter(search_index_version=INDEX_VERSION,search_vector__isnull=False).count()} "
        "for m in (Review,SupportCase)}}))"
    )
    verified = json.loads(
        run(COMPOSE_ARGS + ["exec", "-T", "api", "python", "-c", verify_code]).stdout.strip()
    )
    rows = verified["rows"]
    correct = all(counts["total"] == counts["indexed"] for counts in rows.values())
    support_count = rows["SupportCase"]["total"]
    passed = correct and support_count == imported["fact_counts"]["support_cases"]
    record = {
        "status": "passed" if passed else "failed",
        "entrypoint": "docker compose exec api python manage.py rebuild_evidence_index",
        "compose_project": COMPOSE_PROJECT,
        "elapsed_ms": elapsed_ms,
        "index_version": verified["index_version"],
        "rows": rows,
        "import_report": str(import_report.relative_to(REPOSITORY_ROOT)),
        "import_report_sha256": hashlib.sha256(import_report.read_bytes()).hexdigest(),
        "stdout": build.stdout.strip(),
        "scope": "all review and support evidence imported into the isolated S7 capacity workspace",
        "limitations": [
            "This times index construction for the 1,000 synthetic support cases in this fixture; it is not the target labelled-review corpus build.",
            "It does not establish the 8-core/16-GB/SSD target-server performance gate.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"Result: {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
