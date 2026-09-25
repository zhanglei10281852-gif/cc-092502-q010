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
 event_hash TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,created_at,id);
CREATE INDEX IF NOT EXISTS idx_audit_project ON audit_events(project_id,created_at,id);

-- 研究论证与发布模块 -------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_resources (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('sample','feature','statistic','document','other')),
 ref_code TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1,
 content_hash TEXT NOT NULL,
 summary TEXT NOT NULL,
 content_json TEXT NOT NULL DEFAULT '{}',
 visibility TEXT NOT NULL DEFAULT 'project' CHECK(visibility IN ('project','restricted')),
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','withdrawn')),
 superseded_by_id INTEGER REFERENCES evidence_resources(id) ON DELETE SET NULL,
 withdrawn_reason TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(project_id,ref_code,version)
);
CREATE INDEX IF NOT EXISTS idx_evidence_project ON evidence_resources(project_id,ref_code,version);
CREATE TABLE IF NOT EXISTS claims (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 claim_code TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL,
 statement TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','affected','retracted')),
 affected_reason TEXT NOT NULL DEFAULT '',
 affected_at TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_project ON claims(project_id,status);
CREATE TABLE IF NOT EXISTS claim_edges (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 source_claim_id INTEGER NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
 target_type TEXT NOT NULL CHECK(target_type IN ('evidence','claim')),
 evidence_version_id INTEGER REFERENCES evidence_resources(id) ON DELETE RESTRICT,
 target_claim_id INTEGER REFERENCES claims(id) ON DELETE CASCADE,
 relation TEXT NOT NULL CHECK(relation IN ('supports','rebuts','qualifies')),
 note TEXT NOT NULL DEFAULT '',
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_evidence ON claim_edges(source_claim_id,relation,evidence_version_id) WHERE evidence_version_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_edge_claim ON claim_edges(source_claim_id,relation,target_claim_id) WHERE target_claim_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_edges_source ON claim_edges(source_claim_id);
CREATE INDEX IF NOT EXISTS idx_edges_evidence ON claim_edges(evidence_version_id);
CREATE INDEX IF NOT EXISTS idx_edges_target_claim ON claim_edges(target_claim_id);
CREATE TABLE IF NOT EXISTS review_strategies (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,
 quorum INTEGER NOT NULL CHECK(quorum >= 1),
 eligible_roles_json TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1,
 created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_project ON review_strategies(project_id,active);
CREATE TABLE IF NOT EXISTS review_cycles (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 strategy_id INTEGER REFERENCES review_strategies(id) ON DELETE RESTRICT,
 quorum_snapshot INTEGER NOT NULL,
 eligible_roles_json TEXT NOT NULL,
 open_for_reviews INTEGER NOT NULL DEFAULT 1,
 decided_at TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manuscripts (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 title TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_review','changes_requested','approved','published','withdrawn')),
 author_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 claim_ids_json TEXT NOT NULL DEFAULT '[]',
 content_snapshot_json TEXT NOT NULL DEFAULT '{}',
 review_cycle_id INTEGER REFERENCES review_cycles(id) ON DELETE SET NULL,
 submitted_at TEXT NOT NULL DEFAULT '',
 published_at TEXT NOT NULL DEFAULT '',
 withdrawn_at TEXT NOT NULL DEFAULT '',
 publish_package_json TEXT NOT NULL DEFAULT '{}',
 version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manuscripts_project ON manuscripts(project_id,status);
CREATE TABLE IF NOT EXISTS manuscript_reviews (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 cycle_id INTEGER NOT NULL REFERENCES review_cycles(id) ON DELETE CASCADE,
 manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
 reviewer_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 decision TEXT NOT NULL CHECK(decision IN ('approve','request_changes')),
 comment TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL,
 UNIQUE(cycle_id,reviewer_id)
);
CREATE TABLE IF NOT EXISTS manuscript_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 manuscript_id INTEGER NOT NULL REFERENCES manuscripts(id) ON DELETE CASCADE,
 actor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
 action TEXT NOT NULL,
 from_status TEXT NOT NULL DEFAULT '',
 to_status TEXT NOT NULL DEFAULT '',
 note TEXT NOT NULL DEFAULT '',
 created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_manuscript_events ON manuscript_events(manuscript_id,id);
CREATE TABLE IF NOT EXISTS publications (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 manuscript_id INTEGER NOT NULL UNIQUE REFERENCES manuscripts(id) ON DELETE RESTRICT,
 project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
 package_json TEXT NOT NULL,
 manifest_json TEXT NOT NULL,
 manifest_hash TEXT NOT NULL,
 published_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
 published_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_publications_project ON publications(project_id);
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


def init_db() -> None:
    connection().executescript(SCHEMA)


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
