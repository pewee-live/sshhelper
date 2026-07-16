"""Centralized SQLite database layer.

Provides atomic, concurrent-safe persistence for high-frequency data that was
previously stored in plain JSON files (which had no crash protection, no file
locking, and rewrote entire files on every write).

Tables:
  - audit_log: device operation audit trail (was data/audit/audit.jsonl)
  - device_profiles: device memory/identity (was data/devices/*.json)
  - device_credentials: vault entries (was data/vault/vault.json)

Lower-frequency data (sessions, baselines, cases, device groups) continues to
use JSON files -- those are document-shaped and low-write, so JSON is fine there.
This module focuses on the data that needs transactional safety.

Usage:
    from db import db
    db.record_audit(...)
    db.query_audit(...)
    db.save_profile(...)
    db.get_profile(...)
"""
import sqlite3
import json
import os
import threading
from datetime import datetime
from typing import Optional, List

DB_PATH = os.path.join("data", "app.db")


class Database:
    """Thread-safe SQLite wrapper. Uses a single connection with a write lock
    since SQLite serializes writes anyway; reads are concurrent-safe."""

    def __init__(self, path: str = DB_PATH):
        self._path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")  # crash-safe, concurrent reads
        self._conn.execute("PRAGMA synchronous=NORMAL")  # fast + safe with WAL
        self._init_tables()

    def _init_tables(self):
        with self._lock:
            c = self._conn.cursor()
            c.executescript("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    session_id TEXT,
                    device TEXT,
                    command TEXT,
                    exit_status INTEGER,
                    source TEXT,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_audit_device ON audit_log(device);
                CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
                CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

                CREATE TABLE IF NOT EXISTS device_profiles (
                    device_key TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT
                );

                CREATE TABLE IF NOT EXISTS device_credentials (
                    device_key TEXT PRIMARY KEY,
                    conn_type TEXT,
                    params TEXT,
                    secret_enc TEXT,
                    has_secret INTEGER DEFAULT 0,
                    updated_at TEXT
                );
            """)
            self._conn.commit()

    # --- Audit log ---

    def record_audit(self, session_id: Optional[str], device: str, command: str,
                     exit_status: Optional[int] = None, source: str = "agent",
                     detail: Optional[str] = None):
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO audit_log (ts, session_id, device, command, exit_status, source, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (datetime.now().isoformat(timespec="seconds"), session_id, device,
                     command[:2000], exit_status, source, detail[:500] if detail else None),
                )
                self._conn.commit()
        except Exception as e:
            print(f"[db] audit insert failed: {e}")

    def query_audit(self, session_id: Optional[str] = None, device: Optional[str] = None,
                    limit: int = 200, source: Optional[str] = None) -> List[dict]:
        sql = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if device:
            sql += " AND device = ?"
            params.append(device)
        if source:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(limit, 1000))
        try:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            print(f"[db] audit query failed: {e}")
            return []

    # --- Device profiles ---

    def save_profile(self, device_key: str, data: dict):
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO device_profiles (device_key, data, updated_at) VALUES (?, ?, ?)",
                    (device_key, json.dumps(data, ensure_ascii=False), datetime.now().isoformat()),
                )
                self._conn.commit()
        except Exception as e:
            print(f"[db] profile save failed: {e}")

    def get_profile(self, device_key: str) -> Optional[dict]:
        try:
            row = self._conn.execute(
                "SELECT data FROM device_profiles WHERE device_key = ?", (device_key,)
            ).fetchone()
            return json.loads(row["data"]) if row else None
        except Exception:
            return None

    def list_profiles(self) -> List[dict]:
        try:
            rows = self._conn.execute(
                "SELECT device_key, data FROM device_profiles ORDER BY updated_at DESC"
            ).fetchall()
            return [json.loads(r["data"]) for r in rows]
        except Exception:
            return []

    def delete_profile(self, device_key: str) -> bool:
        with self._lock:
            c = self._conn.execute("DELETE FROM device_profiles WHERE device_key = ?", (device_key,))
            self._conn.commit()
            return c.rowcount > 0

    # --- Device credentials (vault) ---

    def store_credential(self, device_key: str, conn_type: str, params: dict,
                         secret_enc: Optional[str] = None):
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO device_credentials "
                    "(device_key, conn_type, params, secret_enc, has_secret, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (device_key, conn_type, json.dumps(params, ensure_ascii=False),
                     secret_enc, 1 if secret_enc else 0, datetime.now().isoformat()),
                )
                self._conn.commit()
        except Exception as e:
            print(f"[db] credential store failed: {e}")

    def get_credential(self, device_key: str) -> Optional[dict]:
        try:
            row = self._conn.execute(
                "SELECT * FROM device_credentials WHERE device_key = ?", (device_key,)
            ).fetchone()
            if not row:
                return None
            return {
                "device_key": row["device_key"],
                "conn_type": row["conn_type"],
                "params": json.loads(row["params"]),
                "secret_enc": row["secret_enc"],
                "has_secret": bool(row["has_secret"]),
                "updated_at": row["updated_at"],
            }
        except Exception:
            return None

    def list_credentials(self) -> List[dict]:
        try:
            rows = self._conn.execute("SELECT * FROM device_credentials").fetchall()
            return [{
                "device_key": r["device_key"],
                "conn_type": r["conn_type"],
                "params": json.loads(r["params"]),
                "has_secret": bool(r["has_secret"]),
                "updated_at": r["updated_at"],
            } for r in rows]
        except Exception:
            return []

    def delete_credential(self, device_key: str) -> bool:
        with self._lock:
            c = self._conn.execute("DELETE FROM device_credentials WHERE device_key = ?", (device_key,))
            self._conn.commit()
            return c.rowcount > 0

    def close(self):
        with self._lock:
            self._conn.close()


db = Database()