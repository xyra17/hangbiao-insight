#!/usr/bin/env python3
"""Validate the four versioned acceptance packs and write compact audit reports."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
GENERATOR_PATH = SCRIPT_DIR / "05_generate_extended_acceptance_packs.py"
PACKS = (
    "05_近期经营闭环_v1",
    "06_S5动作效果场景_v1",
    "07_S4_S6独立评测_v1",
    "08_S7容量评测_v1",
)
PACK_SELECTIONS = {
    "all": PACKS,
    "loop": (PACKS[0],),
    "actions": PACKS[:2],
    "evaluation": (PACKS[2],),
    "capacity": (PACKS[3],),
}


def load_generator():
    spec = importlib.util.spec_from_file_location("extended_acceptance_packs", GENERATOR_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"Cannot load generator: {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def csv_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def validate_pack_files(output_root: Path, selected_packs: tuple[str, ...]) -> dict:
    checks = []

    def check(name: str, passed: bool, detail: dict | None = None) -> None:
        checks.append({"name": name, "status": "passed" if passed else "failed", "detail": detail or {}})

    for folder in selected_packs:
        package_dir = output_root / folder
        check(f"{folder}:readme", (package_dir / "README.md").is_file())
        check(f"{folder}:field_dictionary", (package_dir / "字段与口径说明.md").is_file())
        check(f"{folder}:manifest", (package_dir / "manifest.json").is_file())

    import_dirs = [output_root / folder / "import_ready" for folder in selected_packs if folder in {PACKS[0], PACKS[3]}]
    sidecars_checked = 0
    sidecars_match = True
    rows_checked = 0
    synthetic_rows = True
    for import_dir in import_dirs:
        for sidecar_path in sorted(import_dir.rglob("*.csv.meta.json")):
            csv_path = Path(str(sidecar_path)[: -len(".meta.json")])
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            row_count = 0
            for row in csv_rows(csv_path):
                row_count += 1
                rows_checked += 1
                synthetic_rows &= row.get("is_real") == "false" and row.get("is_synthetic") == "true"
            sidecars_match &= sidecar.get("row_count") == row_count
            sidecars_checked += 1
    check("import_sidecar_row_counts_match_csv", sidecars_match, {"sidecars": sidecars_checked})
    check("imported_fixture_rows_are_explicitly_synthetic", synthetic_rows, {"rows": rows_checked})

    capacity_manifest_path = output_root / PACKS[3] / "manifest.json"
    if PACKS[3] in selected_packs and capacity_manifest_path.is_file():
        capacity_dir = output_root / PACKS[3]
        capacity_manifest = json.loads(capacity_manifest_path.read_text(encoding="utf-8"))
        version = capacity_manifest.get("package_version")
        markers_match = True
        for sidecar_path in sorted((capacity_dir / "import_ready").rglob("*.csv.meta.json")):
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            markers_match &= sidecar.get("package_version") == version
            csv_path = Path(str(sidecar_path)[: -len(".meta.json")])
            first = next(csv_rows(csv_path), {})
            markers_match &= first.get("generation_version") == version
        check("s7_sidecars_and_rows_use_package_version", markers_match, {"package_version": version})

    check("s7_runner_entry_exists", (SCRIPT_DIR / "run_capacity_acceptance.py").is_file())
    check("paired_decision_scorer_entry_exists", (SCRIPT_DIR / "score_paired_investigations.py").is_file())
    check("capacity_persistent_import_entry_exists", (SCRIPT_DIR / "prepare_capacity_acceptance.py").is_file())
    status = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
    return {"status": status, "checks": checks, "failed": [item["name"] for item in checks if item["status"] != "passed"]}


def main() -> int:
    generator = load_generator()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=SCRIPT_DIR / "data")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None)
    parser.add_argument("--pack", choices=PACK_SELECTIONS, default="all")
    args = parser.parse_args()
    output_root = args.output_root.resolve()
    selected_packs = PACK_SELECTIONS[args.pack]
    if args.as_of:
        as_of = args.as_of
    else:
        manifest = json.loads((output_root / selected_packs[0] / "manifest.json").read_text(encoding="utf-8"))
        as_of = date.fromisoformat(manifest["as_of"])

    generation = generator.validate_generated(output_root, as_of, args.pack)
    package_files = validate_pack_files(output_root, selected_packs)
    report = {
        "status": "passed" if generation["status"] == package_files["status"] == "passed" else "failed",
        "as_of": str(as_of),
        "generation_contract": generation,
        "package_file_contract": package_files,
        "scope_note": "Synthetic fixtures and deterministic checks only; this report is not human-labeled model-quality acceptance or target-environment capacity acceptance.",
    }
    for folder in selected_packs:
        validation_dir = output_root / folder / "validation"
        validation_dir.mkdir(parents=True, exist_ok=True)
        (validation_dir / "unified_validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps({"status": report["status"], "pack": args.pack, "as_of": report["as_of"], "generation_checks": len(generation["checks"]), "file_checks": len(package_files["checks"]), "failed": generation["failed"] + package_files["failed"]}, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
