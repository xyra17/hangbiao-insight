"""Scan release source for credential patterns and local secret values; redact output."""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scan():
    secrets = []
    env = ROOT / ".env.local"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            value = value.strip().strip("\"'")
            if re.search(r"PASSWORD|SECRET|API_KEY|TOKEN", key) and len(value) >= 12 and not value.startswith("${"):
                secrets.append(value)
    patterns = [re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), re.compile(r"(?:ghp_|github_pat_|sk-proj-)[A-Za-z0-9_\-]{20,}")]
    paths = [p for d in ["backend/app", "backend/config", "backend/insight", "frontend/src", "scripts"] for p in (ROOT / d).rglob("*") if p.is_file() and p.suffix in [".py", ".ts", ".tsx", ".css", ".sh"] and "__pycache__" not in p.parts]
    findings = []
    for path in paths:
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            matches = [value for value in secrets if value in line]
            matches.extend(match.group() for pattern in patterns for match in pattern.finditer(line))
            for value in matches:
                findings.append({"path": str(path.relative_to(ROOT)), "line": line_number, "credential_sha256": hashlib.sha256(value.encode()).hexdigest()})
    return {"status": "passed" if not findings else "failed", "files_scanned": len(paths), "findings": findings, "scope": "release source high confidence key patterns and local secret value matching; excludes private environment, raw user data and history; not a third party exhaustive scanner", "secret_values_output": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = scan()
    args.output.write_text(json.dumps(report, indent=2)+"\n")
    raise SystemExit(0 if report["status"] == "passed" else 1)
