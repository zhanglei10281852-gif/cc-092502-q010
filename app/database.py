from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import settings

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 code TEXT NOT NULL UNIQUE,
 name TEXT NOT NULL,
 site_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed','archived')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 username TEXT NOT NULL UNIQUE,
 display_name TEXT NOT NULL,
 password_hash TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_members (
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 role TEXT NOT NULL CHECK(role IN ('owner','researcher','recorder','reviewer','viewer')),
 joined_at TEXT NOT NULL,
 PRIMARY KEY(project_id,user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 token_hash TEXT NOT NULL UNIQUE,
 expires_at TEXT NOT NULL,
 revoked_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL,
 resource_type TEXT NOT NULL,
 resource_id TEXT NOT NULL,
 payload_json TEXT NOT NULL DEFAULT '{}',
 prev_hash TEXT NOT NULL DEFAULT '',
 hash TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_records (
 scope TEXT NOT NULL,
 request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 response_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(scope,request_key)
);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
 job_type TEXT NOT NULL,
 job_key TEXT NOT NULL UNIQUE,
 input_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','leased','retry','done','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0,
 lease_owner TEXT NOT NULL DEFAULT '',
 lease_until TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL DEFAULT '{}',
 error TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('sample','feature','stat')),
 code TEXT NOT NULL,
 title TEXT NOT NULL,
 visibility TEXT NOT NULL DEFAULT 'project' CHECK(visibility IN ('project','restricted')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
 current_version INTEGER NOT NULL DEFAULT 1,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 withdrawn_at TEXT NOT NULL DEFAULT '',
 UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS resource_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 data_json TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(resource_id,version)
);
CREATE TABLE IF NOT EXISTS claims (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 code TEXT NOT NULL,
 title TEXT NOT NULL,
 statement TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,
 affected INTEGER NOT NULL DEFAULT 0,
 affected_reason TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id,code)
);
CREATE TABLE IF NOT EXISTS claim_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,
 title TEXT NOT NULL,
 statement TEXT NOT NULL,
 reason TEXT NOT NULL DEFAULT '',
 changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(claim_id,revision)
);
CREATE TABLE IF NOT EXISTS claim_edges (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 from_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
 edge_type TEXT NOT NULL CHECK(edge_type IN ('supports','rebuts','qualifies')),
 target_claim_id INTEGER REFERENCES claims(id) ON DELETE CASCADE,
 target_resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE,
 target_resource_version INTEGER,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','withdrawn')),
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 withdrawn_at TEXT NOT NULL DEFAULT '',
 CHECK((target_claim_id IS NULL) <> (target_resource_id IS NULL))
);
CREATE TABLE IF NOT EXISTS claim_impacts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
 cause TEXT NOT NULL,
 source_edge_id INTEGER,
 detail TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_policies (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 name TEXT NOT NULL,
 quorum INTEGER NOT NULL CHECK(quorum >= 1),
 reviewer_roles TEXT NOT NULL DEFAULT '["owner","reviewer"]',
 active INTEGER NOT NULL DEFAULT 1,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arguments (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 title TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','submitted','changes_requested','approved','published','retracted')),
 author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 current_revision INTEGER NOT NULL DEFAULT 1,
 policy_snapshot_json TEXT NOT NULL DEFAULT '',
 submitted_at TEXT NOT NULL DEFAULT '',
 published_at TEXT NOT NULL DEFAULT '',
 retracted_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS argument_revisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 argument_id INTEGER NOT NULL REFERENCES arguments(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,
 claim_ids_json TEXT NOT NULL,
 summary TEXT NOT NULL DEFAULT '',
 changed_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(argument_id,revision)
);
CREATE TABLE IF NOT EXISTS reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 argument_id INTEGER NOT NULL REFERENCES arguments(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,
 reviewer_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 decision TEXT NOT NULL CHECK(decision IN ('approve','request_changes')),
 comment TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(argument_id,revision,reviewer_id)
);
CREATE TABLE IF NOT EXISTS publications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 argument_id INTEGER NOT NULL REFERENCES arguments(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,
 package_json TEXT NOT NULL,
 content_hash TEXT NOT NULL,
 manifest_json TEXT NOT NULL,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 UNIQUE(argument_id,revision)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,created_at,id);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events(project_id,created_at,id);
CREATE INDEX IF NOT EXISTS idx_edges_from ON claim_edges(from_claim_id,status);
CREATE INDEX IF NOT EXISTS idx_edges_target_claim ON claim_edges(target_claim_id,status);
CREATE INDEX IF NOT EXISTS idx_edges_target_resource ON claim_edges(target_resource_id,status);
CREATE INDEX IF NOT EXISTS idx_claims_project ON claims(project_id,affected);
CREATE INDEX IF NOT EXISTS idx_impacts_claim ON claim_impacts(claim_id,id);
CREATE INDEX IF NOT EXISTS idx_reviews_argument ON reviews(argument_id,revision);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _create() -> sqlite3.Connection:
    path = settings().database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def connection() -> sqlite3.Connection:
    value = getattr(_local, "connection", None)
    if value is None:
        value = _create()
        _local.connection = value
    return value


def close_connection() -> None:
    value = getattr(_local, "connection", None)
    if value is not None:
        value.close()
        _local.connection = None


def _migrate(db: sqlite3.Connection) -> None:
    """为旧库补齐审计哈希链列并回填，保证重启后校验一致。"""
    columns = {row[1] for row in db.execute("PRAGMA table_info(audit_events)")}
    if "hash" in columns:
        return
    db.execute("ALTER TABLE audit_events ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''")
    db.execute("ALTER TABLE audit_events ADD COLUMN hash TEXT NOT NULL DEFAULT ''")
    from app.security import audit_chain_hash

    previous = ""
    rows = db.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
    for row in rows:
        digest = audit_chain_hash(previous, dict(row))
        db.execute("UPDATE audit_events SET prev_hash=?, hash=? WHERE id=?", (previous, digest, row["id"]))
        previous = digest


def recover_interrupted_jobs(db: sqlite3.Connection) -> int:
    """服务重启时把中断的租约任务退回队列，保证可恢复。"""
    cursor = db.execute("UPDATE jobs SET status='queued',lease_owner='',lease_until='' WHERE status='leased'")
    return cursor.rowcount


def init_db() -> None:
    db = connection()
    db.executescript(SCHEMA)
    _migrate(db)
    recover_interrupted_jobs(db)


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    db = connection()
    db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    else:
        db.commit()
