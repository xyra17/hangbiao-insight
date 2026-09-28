from __future__ import annotations
import csv, hashlib, sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

SCHEMAS: dict[str, dict[str, type]] = {
    "business_metrics": {"product_id": str, "period": str, "orders": float, "refund_rate": float},
    "refund_reasons": {"product_id": str, "period": str, "refund_reason": str, "count": float},
    "after_sales": {"product_id": str, "period": str, "after_sales_code": str, "count": float},
    "inventory": {"product_id": str, "period": str, "stock": float, "out_of_stock_days": float},
}

class DataSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str; name: str; kind: str; status: str; record_count: int = 0
    input_hash: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    quality: dict[str, Any] = Field(default_factory=dict)

class DataSourceRepository:
    def __init__(self, path: str | Path | None = None) -> None:
        root = Path(__file__).resolve().parents[2]
        self.path = Path(path or root / "artifacts" / "data_sources.sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS source_configs (source_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            for kind, schema in SCHEMAS.items():
                columns = ", ".join(f'"{n}" {"REAL" if t is float else "TEXT"} NOT NULL' for n, t in schema.items())
                db.execute(f'CREATE TABLE IF NOT EXISTS "{kind}" (id INTEGER PRIMARY KEY AUTOINCREMENT, {columns}, source_id TEXT NOT NULL, imported_at TEXT NOT NULL)')
    def list(self) -> list[DataSourceConfig]:
        with sqlite3.connect(self.path) as db: rows = db.execute("SELECT payload FROM source_configs ORDER BY source_id").fetchall()
        return [DataSourceConfig.model_validate_json(r[0]) for r in rows]
    def _save(self, c: DataSourceConfig) -> None:
        with sqlite3.connect(self.path) as db: db.execute("INSERT OR REPLACE INTO source_configs VALUES (?,?)", (c.source_id, c.model_dump_json()))
    def import_csv(self, kind: str, filename: str, raw: bytes) -> DataSourceConfig:
        if kind not in SCHEMAS: raise ValueError(f"unsupported data source kind: {kind}")
        schema = SCHEMAS[kind]; reader = csv.DictReader(raw.decode("utf-8-sig").splitlines()); headers = set(reader.fieldnames or [])
        missing = sorted(set(schema) - headers)
        if missing: raise ValueError(f"missing columns: {', '.join(missing)}")
        rows: list[dict[str, Any]] = []; errors: list[dict[str, Any]] = []
        for no, row in enumerate(reader, 2):
            try:
                parsed = {n: (t((row.get(n) or "").strip()) if t is float else (row.get(n) or "").strip()) for n, t in schema.items()}
                if any(parsed[n] == "" for n in schema): raise ValueError("empty required field")
                if len(parsed["period"]) != 7 or parsed["period"][4] != "-": raise ValueError("period must be YYYY-MM")
                if "refund_rate" in parsed and not 0 <= parsed["refund_rate"] <= 1: raise ValueError("refund_rate must be between 0 and 1")
                rows.append(parsed)
            except (TypeError, ValueError) as exc: errors.append({"line": no, "error": str(exc)})
        digest = hashlib.sha256(raw).hexdigest(); source_id = f"upload-{digest[:16]}"; now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self.path) as db:
            db.execute(f'DELETE FROM "{kind}" WHERE source_id=?', (source_id,)); cols = ",".join([*schema, "source_id", "imported_at"]); marks = ",".join("?" for _ in range(len(schema)+2))
            db.executemany(f'INSERT INTO "{kind}" ({cols}) VALUES ({marks})', [tuple(r.values()) + (source_id, now) for r in rows])
        c = DataSourceConfig(source_id=source_id, name=filename, kind=kind, status="ready" if rows and not errors else "ready_with_errors" if rows else "failed", record_count=len(rows), input_hash=digest, quality={"total_rows": len(rows)+len(errors), "accepted_rows": len(rows), "error_rows": len(errors), "errors": errors[:20]})
        self._save(c); return c
    def inspect_sqlite(self, path: str) -> dict[str, Any]:
        p = Path(path).expanduser().resolve()
        if not p.is_file(): raise ValueError("SQLite file does not exist")
        with sqlite3.connect(f"file:{p}?mode=ro", uri=True) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            columns = {t: [r[1] for r in db.execute(f'PRAGMA table_info("{t}")')] for t in tables}
        return {"path": str(p), "tables": tables, "columns": columns}
    def health(self) -> dict[str, Any]:
        configs = self.list(); return {"status": "ready" if configs else "not_configured", "sources": len(configs), "kinds": sorted({c.kind for c in configs}), "database": str(self.path)}
