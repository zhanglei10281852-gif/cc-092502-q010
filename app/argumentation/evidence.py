"""证据资源服务：版本化登记、替换、撤回与可见性投影。"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.argumentation.graph import propagate_impact
from app.database import connection, now, transaction
from app.security import stable_json
from app.service import ResearchService, ServiceError

WRITE_ROLES = {"owner", "researcher", "recorder"}
READ_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}
PRIVILEGED_ROLES = {"owner", "researcher"}


def content_hash(summary: str, content: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json({"summary": summary, "content": content}).encode()).hexdigest()


class EvidenceService(ResearchService):
    def __init__(self, db: sqlite3.Connection | None = None):
        super().__init__(db or connection())

    # -- 可见性 ------------------------------------------------------------
    def can_view_restricted(self, project_id: int, user_id: int) -> bool:
        row = self.db.execute("SELECT role FROM project_members WHERE project_id=? AND user_id=?", (project_id, user_id)).fetchone()
        return bool(row and row["role"] in PRIVILEGED_ROLES)

    def _project_evidence(self, row: sqlite3.Row, privileged: bool) -> dict[str, Any]:
        data = {
            "id": row["id"],
            "kind": row["kind"],
            "ref_code": row["ref_code"],
            "version": row["version"],
            "status": row["status"],
            "visibility": row["visibility"],
            "content_hash": row["content_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        if row["visibility"] == "restricted" and not privileged:
            data["redacted"] = True
            return data
        data.update({
            "summary": row["summary"],
            "content": json.loads(row["content_json"]),
            "superseded_by_id": row["superseded_by_id"],
            "withdrawn_reason": row["withdrawn_reason"],
            "created_by": row["created_by"],
        })
        return data

    def _get_version(self, project_id: int, ref_code: str, version: int | None) -> sqlite3.Row:
        if version is None:
            row = self.db.execute(
                "SELECT * FROM evidence_resources WHERE project_id=? AND ref_code=? ORDER BY version DESC LIMIT 1",
                (project_id, ref_code),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM evidence_resources WHERE project_id=? AND ref_code=? AND version=?",
                (project_id, ref_code, version),
            ).fetchone()
        if row is None:
            raise ServiceError("evidence_not_found", "证据资源不存在", 404)
        return row

    # -- 写入 --------------------------------------------------------------
    def create_evidence(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_role(project_id, actor_id, WRITE_ROLES)
        stamp = now()
        digest = content_hash(payload["summary"], payload.get("content", {}))
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO evidence_resources(project_id,kind,ref_code,version,content_hash,summary,content_json,visibility,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,1,?,?,?,?,?,?,?)",
                    (project_id, payload["kind"], payload["ref_code"], digest, payload["summary"], stable_json(payload.get("content", {})), payload.get("visibility", "project"), actor_id, stamp, stamp),
                )
                self.audit("evidence.create", "evidence", str(cursor.lastrowid), {"ref_code": payload["ref_code"], "version": 1, "content_hash": digest}, project_id=project_id, actor_id=actor_id)
                row = db.execute("SELECT * FROM evidence_resources WHERE id=?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ServiceError("evidence_exists", "同一项目内证据编号与版本已存在", 409) from exc
        return self._project_evidence(row, privileged=True)

    def add_version(self, project_id: int, actor_id: int, ref_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_role(project_id, actor_id, WRITE_ROLES)
        latest = self._get_version(project_id, ref_code, None)
        if latest["status"] == "withdrawn":
            raise ServiceError("evidence_withdrawn", "证据已撤回，不能追加版本", 409)
        stamp = now()
        digest = content_hash(payload["summary"], payload.get("content", {}))
        replace = bool(payload.get("replace"))
        reason = payload.get("reason", "")
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO evidence_resources(project_id,kind,ref_code,version,content_hash,summary,content_json,visibility,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, latest["kind"], ref_code, latest["version"] + 1, digest, payload["summary"], stable_json(payload.get("content", {})), latest["visibility"], actor_id, stamp, stamp),
            )
            new_id = cursor.lastrowid
            affected: list[str] = []
            if replace:
                db.execute(
                    "UPDATE evidence_resources SET status='superseded',superseded_by_id=?,updated_at=? WHERE project_id=? AND ref_code=? AND version<?",
                    (new_id, stamp, project_id, ref_code, latest["version"] + 1),
                )
                affected = propagate_impact(db, project_id, {"type": "evidence", "ref_code": ref_code, "event": "replaced", "reason": reason or f"证据被版本 {latest['version'] + 1} 替换"})
            self.audit(
                "evidence.replace" if replace else "evidence.version",
                "evidence",
                str(new_id),
                {"ref_code": ref_code, "version": latest["version"] + 1, "content_hash": digest, "replaced": replace, "affected_claims": affected},
                project_id=project_id,
                actor_id=actor_id,
            )
            row = db.execute("SELECT * FROM evidence_resources WHERE id=?", (new_id,)).fetchone()
        result = self._project_evidence(row, privileged=True)
        if replace:
            result["affected_claims"] = affected
        return result

    def withdraw(self, project_id: int, actor_id: int, ref_code: str, reason: str) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner", "researcher"})
        latest = self._get_version(project_id, ref_code, None)
        if latest["status"] == "withdrawn":
            raise ServiceError("evidence_withdrawn", "证据已处于撤回状态", 409)
        stamp = now()
        with transaction(immediate=True) as db:
            db.execute(
                "UPDATE evidence_resources SET status='withdrawn',withdrawn_reason=?,updated_at=? WHERE project_id=? AND ref_code=?",
                (reason, stamp, project_id, ref_code),
            )
            affected = propagate_impact(db, project_id, {"type": "evidence", "ref_code": ref_code, "event": "withdrawn", "reason": reason})
            self.audit("evidence.withdraw", "evidence", str(latest["id"]), {"ref_code": ref_code, "reason": reason, "affected_claims": affected}, project_id=project_id, actor_id=actor_id)
        return {"ref_code": ref_code, "status": "withdrawn", "affected_claims": affected}

    # -- 读取 --------------------------------------------------------------
    def list_evidence(self, project_id: int, user_id: int, ref_code: str | None = None) -> list[dict[str, Any]]:
        self.require_role(project_id, user_id, READ_ROLES)
        privileged = self.can_view_restricted(project_id, user_id)
        if ref_code:
            rows = self.db.execute("SELECT * FROM evidence_resources WHERE project_id=? AND ref_code=? ORDER BY version", (project_id, ref_code)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM evidence_resources WHERE project_id=? ORDER BY ref_code,version", (project_id,)).fetchall()
        return [self._project_evidence(row, privileged) for row in rows]

    def get_evidence(self, project_id: int, user_id: int, ref_code: str, version: int | None) -> dict[str, Any]:
        self.require_role(project_id, user_id, READ_ROLES)
        row = self._get_version(project_id, ref_code, version)
        return self._project_evidence(row, self.can_view_restricted(project_id, user_id))

    def diff_versions(self, project_id: int, user_id: int, ref_code: str, from_version: int, to_version: int) -> dict[str, Any]:
        self.require_role(project_id, user_id, READ_ROLES)
        privileged = self.can_view_restricted(project_id, user_id)
        old = self._project_evidence(self._get_version(project_id, ref_code, from_version), privileged)
        new = self._project_evidence(self._get_version(project_id, ref_code, to_version), privileged)
        diff: dict[str, Any] = {"ref_code": ref_code, "from_version": from_version, "to_version": to_version, "changed": []}
        for field in ("kind", "status", "visibility", "summary", "content", "content_hash"):
            old_value, new_value = old.get(field), new.get(field)
            if old_value != new_value:
                diff["changed"].append({"field": field, "from": old_value, "to": new_value})
        return diff
