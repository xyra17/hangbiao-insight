"""Save a reviewable source snapshot without committing or publishing shared work."""
import argparse
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend-image", required=True)
    parser.add_argument("--web-image", required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".local/acceptance-artifacts"):
        parser.error("candidate must stay under local acceptance artifacts")
    output.mkdir(parents=True, exist_ok=True)
    excluded = {".git", ".local", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", "storage", "artifacts", "test-results", "playwright-report", "dist"}
    files = []
    for directory in ["backend", "frontend", "scripts", "dataset/tests"]:
        for path in (ROOT/directory).rglob("*"):
            if path.is_file() and not path.is_symlink() and not any(part in excluded or part.startswith(".env") for part in path.relative_to(ROOT).parts) and path.suffix not in [".pyc", ".png", ".xlsx", ".parquet", ".dump", ".db"]:
                files.append(path)
    files.extend(p for p in (ROOT/"dataset").glob("*.py"))
    files.extend(p for p in ROOT.glob("compose*.yml"))
    files.append(ROOT/"dataset/data/03_黄金评论集/主题分类体系_v1_1.json")
    files.extend(p for p in [ROOT/"dataset/pyproject.toml", ROOT/"dataset/uv.lock"] if p.is_file())
    files.extend(p for p in [ROOT/".dockerignore", ROOT/".env.v2.example", ROOT/".env.example"] if p.is_file())
    hashes = {str(p.relative_to(ROOT)): sha(p) for p in sorted(set(files))}
    archive = output / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as target:
        for name in hashes:
            target.add(ROOT/name, arcname=name, recursive=False)
    images = {}
    for key, name in [("backend", args.backend_image), ("web", args.web_image)]:
        images[key] = {"local_tag": name, "digest": subprocess.check_output(["docker", "image", "inspect", name, "--format", "{{.Id}}"], text=True).strip()}
    code = 'import pathlib,hashlib,json;root=pathlib.Path("/app/backend");print(json.dumps({str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for d in ["insight","config","app"] for p in (root/d).rglob("*.py") if "__pycache__" not in p.parts}))'
    actual = json.loads(subprocess.check_output(["docker", "run", "--rm", "--network", "none", args.backend_image, "python", "-c", code], text=True))
    expected = {name.removeprefix("backend/"): value for name, value in hashes.items() if name.startswith(("backend/insight/", "backend/config/", "backend/app/")) and name.endswith(".py")}
    assert expected == actual, "backend image source differs from saved candidate"
    container = subprocess.check_output(["docker", "create", "--network", "none", args.web_image], text=True).strip()
    web_copy = output/"web-dist"
    web_copy.mkdir(exist_ok=True)
    try:
        subprocess.run(["docker", "cp", container+":/usr/share/nginx/html/.", str(web_copy)], check=True, capture_output=True)
    finally:
        subprocess.run(["docker", "rm", container], check=True, capture_output=True)
    # nginx includes default 50x.html; compare every generated local asset and index.
    dist = ROOT/"frontend/dist"
    web_hashes = {str(p.relative_to(dist)): sha(p) for p in dist.rglob("*") if p.is_file()}
    assert all((web_copy/name).is_file() and sha(web_copy/name) == value for name, value in web_hashes.items()), "web image differs from verified local build"
    report = {"status": "passed", "scope": "local reviewable candidate; not published or deployed, no real-model/target/pilot signoff", "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "shared_tree": "uncommitted snapshot; no bulk commit performed", "source_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), "source_archive_sha256": sha(archive), "source_files": len(hashes), "source_hashes": hashes, "images": images, "backend_source_files_equal": len(expected), "web_dist_files_equal": len(web_hashes), "data_inputs": "separate data_inventory.json hashes; original data excluded from archive", "rollback": "preserve persistent volumes; prior candidate digest/migration compatibility require target owner review; never down -v for production"}
    (output/"candidate.json").write_text(json.dumps(report, indent=2)+"\n")


if __name__ == "__main__":
    main()
