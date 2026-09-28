from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any

from ..agent.state import InvestigationState


class InvestigationRepository:
    def __init__(self, path: str | Path | None = None) -> None:
        default_path = Path(__file__).resolve().parents[2] / "artifacts" / "investigations.sqlite3"
        self.path = Path(path or os.getenv("INVESTIGATION_DB_PATH", str(default_path)))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with sqlite3.connect(self.path) as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS investigations (id TEXT PRIMARY KEY, state_json TEXT NOT NULL, status TEXT NOT NULL, final_answer TEXT, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS investigation_events (investigation_id TEXT NOT NULL, sequence INTEGER NOT NULL, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(investigation_id, sequence));""")
        self.recover_stale()

    def recover_stale(self, timeout_seconds: float = 180.0) -> int:
        """在进程重启后收敛超过总时长的孤儿 running 调查。"""
        now = time.time(); recovered = 0
        with self._lock, sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT id,state_json,updated_at FROM investigations WHERE status='running'").fetchall()
            for iid, raw, updated in rows:
                try: age = now - datetime.fromisoformat(updated).timestamp()
                except Exception: continue
                if age < timeout_seconds: continue
                state = InvestigationState.model_validate_json(raw); state.stop("timeout")
                db.execute("UPDATE investigations SET state_json=?,status=?,updated_at=? WHERE id=?", (state.model_dump_json(), state.status, datetime.now(UTC).isoformat(), iid))
                seq = int(db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM investigation_events WHERE investigation_id=?", (iid,)).fetchone()[0])
                db.execute("INSERT INTO investigation_events VALUES(?,?,?,?,?)", (iid, seq, "investigation_timeout", json.dumps({"reason":"stale_running_recovered"}), datetime.now(UTC).isoformat()))
                recovered += 1
        return recovered

    def save_state(self, state: InvestigationState) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock, sqlite3.connect(self.path) as db:
            db.execute("INSERT INTO investigations(id,state_json,status,final_answer,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json,status=excluded.status,final_answer=excluded.final_answer,updated_at=excluded.updated_at",
                       (state.request_id, state.model_dump_json(), state.status, state.final_answer, now))

    def append_event(self, investigation_id: str, event_type: str, payload: dict[str, Any] | None = None) -> int:
        with self._lock, sqlite3.connect(self.path) as db:
            row = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM investigation_events WHERE investigation_id=?", (investigation_id,)).fetchone()
            seq = int(row[0])
            db.execute("INSERT INTO investigation_events VALUES(?,?,?,?,?)", (investigation_id, seq, event_type, json.dumps(payload or {}, ensure_ascii=False), datetime.now(UTC).isoformat()))
            return seq

    def get(self, investigation_id: str) -> InvestigationState | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT state_json FROM investigations WHERE id=?", (investigation_id,)).fetchone()
        return InvestigationState.model_validate_json(row[0]) if row else None

    def events(self, investigation_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT sequence,event_type,payload_json,created_at FROM investigation_events WHERE investigation_id=? AND sequence>? ORDER BY sequence", (investigation_id, after_sequence)).fetchall()
        return [{"sequence": r[0], "event_type": r[1], "payload": json.loads(r[2]), "created_at": r[3]} for r in rows]
