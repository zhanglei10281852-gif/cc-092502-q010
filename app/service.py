from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import connection, now, transaction
from app.security import audit_chain_hash, expiry, issue_token, password_hash, request_hash, sanitize, stable_json, token_hash, verify_password


class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict[str, Any] | None = None):
        self.code, self.message, self.status, self.details = code, message, status, details or {}
        super().__init__(message)


class BaseService:
    """共享仓储基类：审计哈希链与项目角色检查。所有写方法须在即时事务内调用。"""

    def __init__(self, db: sqlite3.Connection | None = None):
        self.db = db or connection()

    def audit(self, action: str, resource_type: str, resource_id: str, payload: dict[str, Any], *, project_id: int | None = None, actor_id: int | None = None) -> None:
        stamp = now()
        body = stable_json(sanitize(payload))
        previous_row = self.db.execute("SELECT hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        previous = previous_row["hash"] if previous_row else ""
        event = {"project_id": project_id, "actor_id": actor_id, "action": action, "resource_type": resource_type, "resource_id": resource_id, "payload_json": body, "created_at": stamp}
        digest = audit_chain_hash(previous, event)
        self.db.execute(
            "INSERT INTO audit_events(project_id,actor_id,action,resource_type,resource_id,payload_json,prev_hash,hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (project_id, actor_id, action, resource_type, resource_id, body, previous, digest, stamp),
        )

    def require_role(self, project_id: int, user_id: int, allowed: set[str]) -> str:
        row = self.db.execute("SELECT role FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id)).fetchone()
        if row is None or row["role"] not in allowed:
            raise ServiceError("forbidden", "当前用户没有执行该操作的项目权限", 403)
        return row["role"]

    def member_role(self, project_id: int, user_id: int) -> str | None:
        row = self.db.execute("SELECT role FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id)).fetchone()
        return row["role"] if row else None

    def verify_audit_chain(self, project_id: int | None = None) -> dict[str, Any]:
        query = "SELECT * FROM audit_events"
        params: tuple[Any, ...] = ()
        if project_id is not None:
            query += " WHERE project_id=?"
            params = (project_id,)
        rows = self.db.execute(query + " ORDER BY id", params).fetchall()
        previous = ""
        checked = 0
        for row in rows:
            if row["prev_hash"] != previous:
                return {"valid": False, "checked": checked, "first_bad_id": row["id"], "reason": "prev_hash 不连续"}
            if audit_chain_hash(previous, dict(row)) != row["hash"]:
                return {"valid": False, "checked": checked, "first_bad_id": row["id"], "reason": "事件哈希不匹配"}
            previous = row["hash"]
            checked += 1
        return {"valid": True, "checked": checked, "head": previous}


class ResearchService(BaseService):
    def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute("INSERT INTO users(username,display_name,password_hash,created_at,updated_at) VALUES(?,?,?,?,?)", (payload["username"], payload["display_name"], password_hash(payload["password"]), stamp, stamp))
                self.audit("user.create", "user", str(cursor.lastrowid), payload, actor_id=cursor.lastrowid)
                return dict(db.execute("SELECT id,username,display_name,status,created_at FROM users WHERE id=?", (cursor.lastrowid,)).fetchone())
        except sqlite3.IntegrityError as exc:
            raise ServiceError("username_exists", "用户名已存在", 409) from exc

    def login(self, username: str, password: str) -> dict[str, Any]:
        user = self.db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user is None or user["status"] != "active" or not verify_password(password, user["password_hash"]):
            raise ServiceError("invalid_credentials", "用户名或密码错误", 401)
        raw, digest = issue_token()
        with transaction(immediate=True) as db:
            db.execute("INSERT INTO sessions(user_id,token_hash,expires_at,created_at) VALUES(?,?,?,?)", (user["id"], digest, expiry(), now()))
            self.audit("session.login", "user", str(user["id"]), {"username": username}, actor_id=user["id"])
        return {"token": raw, "user_id": user["id"]}

    def authenticate(self, raw: str) -> sqlite3.Row:
        row = self.db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked_at='' AND s.expires_at>? AND u.status='active'", (token_hash(raw), now())).fetchone()
        if row is None:
            raise ServiceError("unauthorized", "会话无效或已过期", 401)
        return row

    def create_project(self, payload: dict[str, Any], owner_id: int, key: str = "") -> dict[str, Any]:
        digest = request_hash(payload)
        if key:
            old = self.db.execute("SELECT * FROM idempotency_records WHERE scope='project.create' AND request_key=?", (key,)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise ServiceError("idempotency_conflict", "幂等键对应的请求内容不同", 409)
                return json.loads(old["response_json"])
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute("INSERT INTO projects(code,name,site_name,created_at,updated_at) VALUES(?,?,?,?,?)", (payload["code"].upper(), payload["name"], payload["site_name"], stamp, stamp))
                project = dict(db.execute("SELECT * FROM projects WHERE id=?", (cursor.lastrowid,)).fetchone())
                db.execute("INSERT INTO project_members(project_id,user_id,role,joined_at) VALUES(?,?,?,?)", (project["id"], owner_id, "owner", stamp))
                self.audit("project.create", "project", str(project["id"]), payload, project_id=project["id"], actor_id=owner_id)
                if key:
                    db.execute("INSERT INTO idempotency_records(scope,request_key,request_hash,response_json,created_at) VALUES('project.create',?,?,?,?)", (key, digest, stable_json(project), stamp))
                return project
        except sqlite3.IntegrityError as exc:
            raise ServiceError("project_exists", "项目编码已存在", 409) from exc

    def add_member(self, project_id: int, actor_id: int, user_id: int, role: str) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner"})
        stamp = now()
        with transaction(immediate=True) as db:
            db.execute("INSERT INTO project_members(project_id,user_id,role,joined_at) VALUES(?,?,?,?) ON CONFLICT(project_id,user_id) DO UPDATE SET role=excluded.role,joined_at=excluded.joined_at", (project_id, user_id, role, stamp))
            self.audit("member.set", "project", str(project_id), {"user_id": user_id, "role": role}, project_id=project_id, actor_id=actor_id)
        return {"project_id": project_id, "user_id": user_id, "role": role}

    def enqueue(self, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        if payload.get("project_id"):
            self.require_role(payload["project_id"], actor_id, {"owner", "researcher", "recorder", "reviewer"})
        stamp = now()
        with transaction(immediate=True) as db:
            old = db.execute("SELECT * FROM jobs WHERE job_key=?", (payload["job_key"],)).fetchone()
            if old:
                return dict(old)
            cursor = db.execute("INSERT INTO jobs(project_id,job_type,job_key,input_json,created_at,updated_at) VALUES(?,?,?,?,?,?)", (payload.get("project_id"), payload["job_type"], payload["job_key"], stable_json(payload.get("input", {})), stamp, stamp))
            self.audit("job.enqueue", "job", str(cursor.lastrowid), payload, project_id=payload.get("project_id"), actor_id=actor_id)
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (cursor.lastrowid,)).fetchone())

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        with transaction(immediate=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE status IN ('queued','retry') ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return None
            stamp = now()
            db.execute("UPDATE jobs SET status='leased',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE id=?", (worker_id, stamp, stamp, row["id"]))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def finish(self, job_id: int, worker_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as db:
            row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] != "leased" or row["lease_owner"] != worker_id:
                raise ServiceError("job_not_owned", "任务不存在或不属于该工作者", 409)
            db.execute("UPDATE jobs SET status='done',result_json=?,lease_owner='',lease_until='',updated_at=? WHERE id=?", (stable_json(result), now(), job_id))
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
