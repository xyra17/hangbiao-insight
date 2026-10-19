#!/usr/bin/env python3
"""Build a frozen replay or a date-current demo pack in a versioned run directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
RUNS_ROOT = SCRIPT_DIR / "data" / "acceptance_runs"
SITE_ZONE = ZoneInfo("America/Los_Angeles")
GENERATOR = SCRIPT_DIR / "05_generate_extended_acceptance_packs.py"
VALIDATOR = SCRIPT_DIR / "validate_acceptance_packs.py"
FROZEN_DEFAULT = date(2026, 10, 1)
PACK_CHOICES = ("loop", "actions", "evaluation", "capacity", "all")
PACK_DIRS = {
    "loop": ("05_近期经营闭环_v1",),
    "actions": ("05_近期经营闭环_v1", "06_S5动作效果场景_v1"),
    "evaluation": ("07_S4_S6独立评测_v1",),
    "capacity": ("08_S7容量评测_v1",),
    "all": (
        "05_近期经营闭环_v1",
        "06_S5动作效果场景_v1",
        "07_S4_S6独立评测_v1",
        "08_S7容量评测_v1",
    ),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_dates(mode: str, requested: date | None, site_today: date) -> tuple[date, str]:
    if mode == "frozen-replay":
        as_of = requested or FROZEN_DEFAULT
        if as_of > site_today:
            raise ValueError("Frozen replay as_of cannot be in the future.")
        return as_of, "frozen"
    as_of = requested or site_today
    if as_of != site_today:
        raise ValueError("current-demo as_of must match the current America/Los_Angeles site date.")
    return as_of, "current"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("frozen-replay", "current-demo"), required=True)
    parser.add_argument("--as-of", type=date.fromisoformat)
    parser.add_argument("--pack", choices=PACK_CHOICES, default="actions")
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()

    site_today = datetime.now(SITE_ZONE).date()
    try:
        as_of, clock_mode = resolve_dates(args.mode, args.as_of, site_today)
    except ValueError as exc:
        parser.error(str(exc))

    run_name = f"{args.mode}_{as_of.isoformat()}"
    output_root = (args.output_root or RUNS_ROOT / run_name).resolve()
    allowed_root = RUNS_ROOT.resolve()
    if output_root != allowed_root and allowed_root not in output_root.parents:
        parser.error(f"output-root must stay under {allowed_root}")
    if output_root.exists() and any(output_root.iterdir()):
        parser.error(f"output-root already contains files; choose a new versioned path: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.combine(as_of, time(18, 0), UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    commands = [
        [
            sys.executable, str(GENERATOR), "--pack", args.pack, "--as-of", str(as_of),
            "--seed", str(args.seed), "--generated-at", generated_at, "--output-root", str(output_root),
        ],
        [
            sys.executable, str(VALIDATOR), "--pack", args.pack, "--as-of", str(as_of),
            "--output-root", str(output_root),
        ],
    ]
    started_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    results = []
    status = "passed"
    for command_index, command in enumerate(commands):
        result = subprocess.run(command, cwd=REPO_ROOT, check=False, capture_output=True, text=True)
        results.append({
            "command": command,
            "exit_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        })
        if result.returncode != 0:
            status = "failed"
            break
        if command_index == 0:
            for folder in PACK_DIRS[args.pack]:
                source_dir = SCRIPT_DIR / "data" / folder
                output_dir = output_root / folder
                shutil.copy2(source_dir / "字段与口径说明.md", output_dir / "字段与口径说明.md")
                title = folder.replace("_v1", "").replace("_", " ")
                (output_dir / "README.md").write_text(
                    f"# {title}\n\n"
                    f"- 运行模式：`{args.mode}`\n"
                    f"- 冻结 `as_of`：`{as_of}`\n"
                    f"- 运行时站点日期：`{site_today}`（America/Los_Angeles）\n"
                    f"- 生成时间：`{generated_at}`\n"
                    f"- 随机种子：`{args.seed}`\n"
                    "- 数据性质：合成验收数据，不是真实运营事实或人工标注。\n"
                    "- 字段与口径：见同目录 `字段与口径说明.md`。\n"
                    "- 文件哈希及完整执行命令：见上级 `run_manifest.json`。\n",
                    encoding="utf-8",
                )

    files = {
        str(path.relative_to(output_root)): {"bytes": path.stat().st_size, "sha256": sha256(path)}
        for path in sorted(output_root.rglob("*"))
        if path.is_file() and path.name != "run_manifest.json"
    }
    run_manifest = {
        "status": status,
        "mode": args.mode,
        "clock_mode": clock_mode,
        "frozen_as_of": str(as_of),
        "runtime_site_date": str(site_today),
        "site_timezone": "America/Los_Angeles",
        "generated_at": generated_at,
        "started_at": started_at,
        "pack": args.pack,
        "seed": args.seed,
        "generator_sha256": sha256(GENERATOR),
        "commands": results,
        "file_manifest": files,
        "scope_note": (
            "Generated fixtures are synthetic. frozen_as_of controls generated business dates; "
            "runtime_site_date records the real site-local date when this command ran. "
            "This run does not represent live data freshness or human/model acceptance."
        ),
    }
    (output_root / "run_manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": status,
        "mode": args.mode,
        "frozen_as_of": str(as_of),
        "runtime_site_date": str(site_today),
        "pack": args.pack,
        "file_count": len(files),
        "output_root": str(output_root),
        "run_manifest": str(output_root / "run_manifest.json"),
    }, ensure_ascii=False, indent=2))
    return 0 if status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
