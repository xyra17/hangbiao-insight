"""SQLite backup/restore utility for local Core MVP releases."""
from __future__ import annotations
import argparse, os, sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
DEFAULT = ARTIFACTS / "investigations.sqlite3"
DATABASES = {"investigations.sqlite3": "INVESTIGATION_DB_PATH", "claim_store.sqlite3": "CLAIM_STORE_DB_PATH"}

def copy_db(source: Path, target: Path) -> None:
    if not source.exists():
        raise SystemExit(f"database not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)

def main() -> None:
    parser = argparse.ArgumentParser(description="Backup or restore Core MVP SQLite state")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("backup", "restore", "backup-all", "restore-all"):
        p = sub.add_parser(name); p.add_argument("path", type=Path)
    args = parser.parse_args()
    db = Path(os.getenv("INVESTIGATION_DB_PATH", DEFAULT))
    if args.command == "backup": copy_db(db, args.path)
    elif args.command == "restore": copy_db(args.path, db)
    else:
        args.path.mkdir(parents=True, exist_ok=True)
        for filename, env_name in DATABASES.items():
            live = Path(os.getenv(env_name, ARTIFACTS / filename))
            if args.command == "backup-all": copy_db(live, args.path / filename)
            else: copy_db(args.path / filename, live)
    print(f"{args.command}: {args.path}")

if __name__ == "__main__": main()
