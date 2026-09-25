"""论点与论证边服务：原子论点、类型化依赖边、环路拒绝。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.argumentation.evidence import READ_ROLES, WRITE_ROLES, EvidenceService
from app.argumentation.graph import find_cycle_path, propagate_impact
from app.database import connection, now, transaction
from app.service import ResearchService, ServiceError


class ClaimService(ResearchService):
    def __init__(self, db: sqlite3.Connection | None = None):
        super().__init__(db or connection())
        self.evidence = EvidenceService(self.db)

    # -- 论点 --------------------------------------------------------------
    def create_claim(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_role(project_id, actor_id, WRITE_ROLES)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO claims(project_id,claim_code,title,statement,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (project_id, payload["claim_code"], payload["title"], payload["statement"], actor_id, stamp, stamp),
                )
                self.audit("claim.create", "claim", str(cursor.lastrowid), payload, project_id=project_id, actor_id=actor_id)
                row = db.execute("SELECT * FROM claims WHERE id=?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ServiceError("claim_exists", "论点编码已存在", 409) from exc
        return self._project_claim(row, project_id, actor_id)

    def retract_claim(self, project_id: int, actor_id: int, claim_code: str, reason: str) -> dict[str, Any]:
        self.require_role(project_id, actor_id, WRITE_ROLES)
        row = self._get_claim(project_id, claim_code)
        if row["status"] == "retracted":
            raise ServiceError("claim_retracted", "论点已撤回", 409)
        stamp = now()
        with transaction(immediate=True) as db:
            db.execute("UPDATE claims SET status='retracted',affected_reason='',updated_at=? WHERE id=?", (stamp, row["id"]))
            affected = propagate_impact(db, project_id, {"type": "claim", "claim_id": row["id"], "claim_code": claim_code, "event": "retracted", "reason": reason})
            self.audit("claim.retract", "claim", str(row["id"]), {"claim_code": claim_code, "reason": reason, "affected_claims": affected}, project_id=project_id, actor_id=actor_id)
        return {"claim_code": claim_code, "status": "retracted", "affected_claims": affected}

    def _get_claim(self, project_id: int, claim_code: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM claims WHERE project_id=? AND claim_code=?", (project_id, claim_code)).fetchone()
        if row is None:
            raise ServiceError("claim_not_found", "论点不存在", 404)
        return row

    def _project_claim(self, row: sqlite3.Row, project_id: int, user_id: int) -> dict[str, Any]:
        privileged = self.evidence.can_view_restricted(project_id, user_id)
        reasons = json.loads(row["affected_reason"]) if row["affected_reason"] else []
        if not privileged:
            reasons = [item for item in reasons if item.get("type") != "evidence" or self._evidence_visible(project_id, item.get("ref_code", ""), privileged)]
        return {
            "id": row["id"],
            "claim_code": row["claim_code"],
            "title": row["title"],
            "statement": row["statement"],
            "status": row["status"],
            "affected_reasons": reasons,
            "affected_at": row["affected_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _evidence_visible(self, project_id: int, ref_code: str, privileged: bool) -> bool:
        if privileged:
            return True
        row = self.db.execute("SELECT visibility FROM evidence_resources WHERE project_id=? AND ref_code=? ORDER BY version DESC LIMIT 1", (project_id, ref_code)).fetchone()
        return bool(row and row["visibility"] == "project")

    def list_claims(self, project_id: int, user_id: int) -> list[dict[str, Any]]:
        self.require_role(project_id, user_id, READ_ROLES)
        rows = self.db.execute("SELECT * FROM claims WHERE project_id=? ORDER BY claim_code", (project_id,)).fetchall()
        return [self._project_claim(row, project_id, user_id) for row in rows]

    def get_claim(self, project_id: int, user_id: int, claim_code: str) -> dict[str, Any]:
        self.require_role(project_id, user_id, READ_ROLES)
        return self._project_claim(self._get_claim(project_id, claim_code), project_id, user_id)

    # -- 论证边 --------------------------------------------------------------
    def add_edge(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_role(project_id, actor_id, WRITE_ROLES)
        source = self._get_claim(project_id, payload["source_claim_code"])
        if source["status"] == "retracted":
            raise ServiceError("claim_retracted", "不能为已撤回论点添加依赖边", 409)
        evidence_version_id: int | None = None
        target_claim_id: int | None = None
        target_type: str
        if payload.get("evidence_ref"):
            target_type = "evidence"
            evidence = self.evidence._get_version(project_id, payload["evidence_ref"], payload.get("evidence_version"))
            evidence_version_id = evidence["id"]
        elif payload.get("target_claim_code"):
            target_type = "claim"
            target = self._get_claim(project_id, payload["target_claim_code"])
            if target["status"] == "retracted":
                raise ServiceError("claim_retracted", "不能依赖已撤回的论点", 409)
            target_claim_id = target["id"]
            cycle = find_cycle_path(self.db, source["id"], target_claim_id)
            if cycle is not None:
                raise ServiceError("dependency_cycle", "新增依赖会形成论点环路", 409, details={"cycle_path": cycle})
        else:
            raise ServiceError("edge_target_required", "必须指定证据或目标论点", 400)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO claim_edges(project_id,source_claim_id,target_type,evidence_version_id,target_claim_id,relation,note,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (project_id, source["id"], target_type, evidence_version_id, target_claim_id, payload["relation"], payload.get("note", ""), actor_id, stamp),
                )
                self.audit(
                    "edge.create",
                    "edge",
                    str(cursor.lastrowid),
                    {"source": payload["source_claim_code"], "relation": payload["relation"], "evidence_ref": payload.get("evidence_ref"), "evidence_version": payload.get("evidence_version"), "target_claim": payload.get("target_claim_code")},
                    project_id=project_id,
                    actor_id=actor_id,
                )
                row = db.execute("SELECT * FROM claim_edges WHERE id=?", (cursor.lastrowid,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise ServiceError("edge_exists", "相同的依赖边已存在", 409) from exc
        return self._project_edge(row, project_id, actor_id)

    def _project_edge(self, row: sqlite3.Row, project_id: int, user_id: int) -> dict[str, Any]:
        privileged = self.evidence.can_view_restricted(project_id, user_id)
        data: dict[str, Any] = {
            "id": row["id"],
            "relation": row["relation"],
            "note": row["note"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        source = self.db.execute("SELECT claim_code FROM claims WHERE id=?", (row["source_claim_id"],)).fetchone()
        data["source_claim_code"] = source["claim_code"] if source else None
        if row["target_type"] == "evidence":
            evidence = self.db.execute("SELECT * FROM evidence_resources WHERE id=?", (row["evidence_version_id"],)).fetchone()
            data["target_type"] = "evidence"
            data["evidence_ref"] = evidence["ref_code"]
            data["evidence_version"] = evidence["version"]
            data["evidence_status"] = evidence["status"]
            data["evidence_content_hash"] = evidence["content_hash"]
            if evidence["visibility"] == "restricted" and not privileged:
                data["evidence_redacted"] = True
            else:
                data["evidence_summary"] = evidence["summary"]
        else:
            target = self.db.execute("SELECT claim_code,status FROM claims WHERE id=?", (row["target_claim_id"],)).fetchone()
            data["target_type"] = "claim"
            data["target_claim_code"] = target["claim_code"] if target else None
            data["target_claim_status"] = target["status"] if target else None
        return data

    def list_edges(self, project_id: int, user_id: int, claim_code: str | None = None) -> list[dict[str, Any]]:
        self.require_role(project_id, user_id, READ_ROLES)
        if claim_code:
            claim = self._get_claim(project_id, claim_code)
            rows = self.db.execute("SELECT * FROM claim_edges WHERE project_id=? AND source_claim_id=? ORDER BY id", (project_id, claim["id"])).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM claim_edges WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._project_edge(row, project_id, user_id) for row in rows]

    def affected_claims(self, project_id: int, user_id: int) -> list[dict[str, Any]]:
        self.require_role(project_id, user_id, READ_ROLES)
        rows = self.db.execute("SELECT * FROM claims WHERE project_id=? AND status='affected' ORDER BY claim_code", (project_id,)).fetchall()
        return [self._project_claim(row, project_id, user_id) for row in rows]
