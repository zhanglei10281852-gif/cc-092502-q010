"""论证稿服务：草拟、送审、要求修改、批准、发布、撤回的状态机。

关键不变量：
- 作者不能批准自己的稿件；
- 法定人数与合格角色在送审时快照，评审策略随后修改不影响进行中的评审；
- 每位评审人在同一评审轮次只能投一票（数据库唯一约束兜底），重复回调返回既有结果；
- 发布通过稿件状态条件更新与 publications 表唯一约束双重保证只发生一次。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.argumentation.evidence import READ_ROLES, EvidenceService
from app.argumentation.packaging import build_manifest, build_package, manifest_hash, project_manifest, project_package
from app.database import connection, now, transaction
from app.security import stable_json
from app.service import ResearchService, ServiceError

WRITE_ROLES = {"owner", "researcher"}
ALL_ROLES = {"owner", "researcher", "recorder", "reviewer", "viewer"}


class ManuscriptService(ResearchService):
    def __init__(self, db: sqlite3.Connection | None = None):
        super().__init__(db or connection())
        self.evidence = EvidenceService(self.db)

    # -- 评审策略 ------------------------------------------------------------
    def set_strategy(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner"})
        roles = sorted(set(payload["eligible_roles"]))
        invalid = [role for role in roles if role not in ALL_ROLES]
        if invalid:
            raise ServiceError("invalid_roles", f"未知角色: {','.join(invalid)}", 400)
        stamp = now()
        with transaction(immediate=True) as db:
            last = db.execute("SELECT MAX(version) AS v FROM review_strategies WHERE project_id=?", (project_id,)).fetchone()
            version = (last["v"] or 0) + 1
            db.execute("UPDATE review_strategies SET active=0 WHERE project_id=?", (project_id,))
            cursor = db.execute(
                "INSERT INTO review_strategies(project_id,version,quorum,eligible_roles_json,active,created_by,created_at) VALUES(?,?,?,?,1,?,?)",
                (project_id, version, payload["quorum"], stable_json(roles), actor_id, stamp),
            )
            self.audit("strategy.set", "review_strategy", str(cursor.lastrowid), {"quorum": payload["quorum"], "eligible_roles": roles}, project_id=project_id, actor_id=actor_id)
        return {"project_id": project_id, "version": version, "quorum": payload["quorum"], "eligible_roles": roles}

    def get_strategy(self, project_id: int, user_id: int) -> dict[str, Any]:
        self.require_role(project_id, user_id, READ_ROLES)
        row = self.db.execute("SELECT * FROM review_strategies WHERE project_id=? AND active=1", (project_id,)).fetchone()
        if row is None:
            return {"project_id": project_id, "version": 0, "quorum": 1, "eligible_roles": ["reviewer"]}
        return {"project_id": project_id, "version": row["version"], "quorum": row["quorum"], "eligible_roles": json.loads(row["eligible_roles_json"])}

    # -- 稿件 --------------------------------------------------------------
    def _get_manuscript(self, project_id: int, manuscript_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM manuscripts WHERE id=? AND project_id=?", (manuscript_id, project_id)).fetchone()
        if row is None:
            raise ServiceError("manuscript_not_found", "稿件不存在", 404)
        return row

    def _claim_ids(self, project_id: int, claim_codes: list[str]) -> list[int]:
        ids: list[int] = []
        for code in claim_codes:
            row = self.db.execute("SELECT id,status FROM claims WHERE project_id=? AND claim_code=?", (project_id, code)).fetchone()
            if row is None:
                raise ServiceError("claim_not_found", f"论点不存在: {code}", 404)
            if row["status"] == "retracted":
                raise ServiceError("claim_retracted", f"论点已撤回，不能纳入稿件: {code}", 409)
            ids.append(row["id"])
        return ids

    def create_manuscript(self, project_id: int, actor_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        self.require_role(project_id, actor_id, WRITE_ROLES)
        ids = self._claim_ids(project_id, payload["claim_codes"])
        stamp = now()
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO manuscripts(project_id,title,author_id,claim_ids_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (project_id, payload["title"], actor_id, stable_json(ids), stamp, stamp),
            )
            manuscript_id = cursor.lastrowid
            self._event(db, manuscript_id, actor_id, "create", "", "draft", "")
            self.audit("manuscript.create", "manuscript", str(manuscript_id), {"title": payload["title"], "claim_codes": payload["claim_codes"]}, project_id=project_id, actor_id=actor_id)
        return self.get_manuscript(project_id, actor_id, manuscript_id)

    def update_manuscript(self, project_id: int, actor_id: int, manuscript_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._get_manuscript(project_id, manuscript_id)
        self._require_author_or_owner(project_id, actor_id, row)
        if row["status"] not in ("draft", "changes_requested"):
            raise ServiceError("invalid_state", "仅草稿或要求修改状态可以编辑", 409)
        title = payload.get("title") or row["title"]
        ids = self._claim_ids(project_id, payload["claim_codes"]) if payload.get("claim_codes") else json.loads(row["claim_ids_json"])
        with transaction(immediate=True) as db:
            db.execute("UPDATE manuscripts SET title=?,claim_ids_json=?,updated_at=? WHERE id=?", (title, stable_json(ids), now(), manuscript_id))
            self._event(db, manuscript_id, actor_id, "edit", row["status"], row["status"], "")
            self.audit("manuscript.edit", "manuscript", str(manuscript_id), {"title": title}, project_id=project_id, actor_id=actor_id)
        return self.get_manuscript(project_id, actor_id, manuscript_id)

    def _require_author_or_owner(self, project_id: int, actor_id: int, row: sqlite3.Row) -> None:
        role = self.require_role(project_id, actor_id, WRITE_ROLES)
        if row["author_id"] != actor_id and role != "owner":
            raise ServiceError("forbidden", "仅作者或项目负责人可以执行该操作", 403)

    def _event(self, db: sqlite3.Connection, manuscript_id: int, actor_id: int | None, action: str, from_status: str, to_status: str, note: str) -> None:
        db.execute(
            "INSERT INTO manuscript_events(manuscript_id,actor_id,action,from_status,to_status,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (manuscript_id, actor_id, action, from_status, to_status, note, now()),
        )

    def submit(self, project_id: int, actor_id: int, manuscript_id: int) -> dict[str, Any]:
        row = self._get_manuscript(project_id, manuscript_id)
        self._require_author_or_owner(project_id, actor_id, row)
        if row["status"] not in ("draft", "changes_requested"):
            raise ServiceError("invalid_state", f"当前状态 {row['status']} 不能送审", 409)
        strategy = self.get_strategy(project_id, actor_id)
        strategy_row = self.db.execute("SELECT id FROM review_strategies WHERE project_id=? AND active=1", (project_id,)).fetchone()
        stamp = now()
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO review_cycles(project_id,strategy_id,quorum_snapshot,eligible_roles_json,created_at) VALUES(?,?,?,?,?)",
                (project_id, strategy_row["id"] if strategy_row else None, strategy["quorum"], stable_json(strategy["eligible_roles"]), stamp),
            )
            cycle_id = cursor.lastrowid
            db.execute(
                "UPDATE manuscripts SET status='in_review',review_cycle_id=?,submitted_at=?,version=version+1,updated_at=? WHERE id=?",
                (cycle_id, stamp, stamp, manuscript_id),
            )
            self._event(db, manuscript_id, actor_id, "submit", row["status"], "in_review", f"quorum={strategy['quorum']}")
            self.audit("manuscript.submit", "manuscript", str(manuscript_id), {"cycle_id": cycle_id, "quorum": strategy["quorum"], "eligible_roles": strategy["eligible_roles"]}, project_id=project_id, actor_id=actor_id)
        return self.get_manuscript(project_id, actor_id, manuscript_id)

    # -- 评审 --------------------------------------------------------------
    def review(self, project_id: int, actor_id: int, manuscript_id: int, decision: str, comment: str) -> dict[str, Any]:
        row = self._get_manuscript(project_id, manuscript_id)
        if row["review_cycle_id"]:
            # 重复回调优先于状态校验：同一评审轮次已投过票则返回既有结果，不产生第二次效果
            existing = self.db.execute("SELECT * FROM manuscript_reviews WHERE cycle_id=? AND reviewer_id=?", (row["review_cycle_id"], actor_id)).fetchone()
            if existing is not None:
                return {"manuscript_id": manuscript_id, "duplicate": True, "decision": existing["decision"], "status": row["status"]}
        if row["status"] != "in_review":
            raise ServiceError("invalid_state", f"当前状态 {row['status']} 不能评审", 409)
        if row["author_id"] == actor_id:
            raise ServiceError("self_approval", "作者不能批准自己的稿件", 403)
        cycle = self.db.execute("SELECT * FROM review_cycles WHERE id=?", (row["review_cycle_id"],)).fetchone()
        eligible = set(json.loads(cycle["eligible_roles_json"]))
        role = self.require_role(project_id, actor_id, READ_ROLES)
        if role not in eligible:
            raise ServiceError("not_eligible", "当前角色不在评审策略快照的合格角色中", 403)
        stamp = now()
        with transaction(immediate=True) as db:
            existing = db.execute("SELECT * FROM manuscript_reviews WHERE cycle_id=? AND reviewer_id=?", (cycle["id"], actor_id)).fetchone()
            if existing is not None:
                return {"manuscript_id": manuscript_id, "duplicate": True, "decision": existing["decision"], "status": self._status(db, manuscript_id)}
            fresh_cycle = db.execute("SELECT * FROM review_cycles WHERE id=?", (cycle["id"],)).fetchone()
            if not fresh_cycle["open_for_reviews"]:
                raise ServiceError("cycle_closed", "评审轮次已关闭", 409)
            db.execute(
                "INSERT INTO manuscript_reviews(cycle_id,manuscript_id,reviewer_id,decision,comment,created_at) VALUES(?,?,?,?,?,?)",
                (cycle["id"], manuscript_id, actor_id, decision, comment, stamp),
            )
            self.audit("manuscript.review", "manuscript", str(manuscript_id), {"cycle_id": cycle["id"], "decision": decision, "comment": comment}, project_id=project_id, actor_id=actor_id)
            if decision == "request_changes":
                db.execute("UPDATE review_cycles SET open_for_reviews=0,decided_at=? WHERE id=?", (stamp, cycle["id"]))
                changed = db.execute("UPDATE manuscripts SET status='changes_requested',updated_at=? WHERE id=? AND status='in_review'", (stamp, manuscript_id)).rowcount
                if changed:
                    self._event(db, manuscript_id, actor_id, "request_changes", "in_review", "changes_requested", comment)
                return {"manuscript_id": manuscript_id, "duplicate": False, "decision": decision, "status": self._status(db, manuscript_id)}
            approvals = db.execute("SELECT COUNT(*) AS n FROM manuscript_reviews WHERE cycle_id=? AND decision='approve'", (cycle["id"],)).fetchone()["n"]
            if approvals >= cycle["quorum_snapshot"]:
                # 并发批准下只有第一个把状态从 in_review 改走的事务生效
                changed = db.execute("UPDATE manuscripts SET status='approved',updated_at=? WHERE id=? AND status='in_review'", (stamp, manuscript_id)).rowcount
                db.execute("UPDATE review_cycles SET open_for_reviews=0,decided_at=? WHERE id=?", (stamp, cycle["id"]))
                if changed:
                    self._event(db, manuscript_id, actor_id, "approve", "in_review", "approved", f"approvals={approvals}")
                return {"manuscript_id": manuscript_id, "duplicate": False, "decision": decision, "status": "approved", "approvals": approvals}
            return {"manuscript_id": manuscript_id, "duplicate": False, "decision": decision, "status": "in_review", "approvals": approvals}

    def _status(self, db: sqlite3.Connection, manuscript_id: int) -> str:
        return db.execute("SELECT status FROM manuscripts WHERE id=?", (manuscript_id,)).fetchone()["status"]

    # -- 发布与撤回 ----------------------------------------------------------
    def publish(self, project_id: int, actor_id: int, manuscript_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner", "reviewer"})
        row = self._get_manuscript(project_id, manuscript_id)
        if row["status"] == "published":
            publication = self.db.execute("SELECT * FROM publications WHERE manuscript_id=?", (manuscript_id,)).fetchone()
            return {"manuscript_id": manuscript_id, "status": "published", "duplicate": True, "manifest_hash": publication["manifest_hash"]}
        if row["status"] != "approved":
            raise ServiceError("invalid_state", f"当前状态 {row['status']} 不能发布", 409)
        stamp = now()
        with transaction(immediate=True) as db:
            # 并发发布：只有一个事务能把 approved → published
            changed = db.execute("UPDATE manuscripts SET status='published',published_at=?,updated_at=? WHERE id=? AND status='approved'", (stamp, stamp, manuscript_id)).rowcount
            if not changed:
                publication = db.execute("SELECT * FROM publications WHERE manuscript_id=?", (manuscript_id,)).fetchone()
                return {"manuscript_id": manuscript_id, "status": "published", "duplicate": True, "manifest_hash": publication["manifest_hash"]}
            fresh = db.execute("SELECT * FROM manuscripts WHERE id=?", (manuscript_id,)).fetchone()
            package = build_package(db, fresh)
            manifest = build_manifest(package)
            digest = manifest_hash(manifest)
            db.execute(
                "INSERT INTO publications(manuscript_id,project_id,package_json,manifest_json,manifest_hash,published_by,published_at) VALUES(?,?,?,?,?,?,?)",
                (manuscript_id, project_id, stable_json(package), stable_json(manifest), digest, actor_id, stamp),
            )
            db.execute("UPDATE manuscripts SET publish_package_json=? WHERE id=?", (stable_json({"content_hash": package["content_hash"], "manifest_hash": digest}), manuscript_id))
            self._event(db, manuscript_id, actor_id, "publish", "approved", "published", digest)
            self.audit("manuscript.publish", "manuscript", str(manuscript_id), {"manifest_hash": digest, "content_hash": package["content_hash"]}, project_id=project_id, actor_id=actor_id)
        return {"manuscript_id": manuscript_id, "status": "published", "duplicate": False, "manifest_hash": digest}

    def withdraw(self, project_id: int, actor_id: int, manuscript_id: int, reason: str) -> dict[str, Any]:
        row = self._get_manuscript(project_id, manuscript_id)
        self._require_author_or_owner(project_id, actor_id, row)
        if row["status"] != "published":
            raise ServiceError("invalid_state", "仅已发布稿件可以撤回", 409)
        stamp = now()
        with transaction(immediate=True) as db:
            db.execute("UPDATE manuscripts SET status='withdrawn',withdrawn_at=?,updated_at=? WHERE id=?", (stamp, stamp, manuscript_id))
            self._event(db, manuscript_id, actor_id, "withdraw", "published", "withdrawn", reason)
            self.audit("manuscript.withdraw", "manuscript", str(manuscript_id), {"reason": reason}, project_id=project_id, actor_id=actor_id)
        return {"manuscript_id": manuscript_id, "status": "withdrawn"}

    # -- 读取 --------------------------------------------------------------
    def get_manuscript(self, project_id: int, user_id: int, manuscript_id: int) -> dict[str, Any]:
        self.require_role(project_id, user_id, READ_ROLES)
        row = self._get_manuscript(project_id, manuscript_id)
        return self._project_manuscript(row, project_id, user_id)

    def list_manuscripts(self, project_id: int, user_id: int) -> list[dict[str, Any]]:
        self.require_role(project_id, user_id, READ_ROLES)
        rows = self.db.execute("SELECT * FROM manuscripts WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._project_manuscript(row, project_id, user_id) for row in rows]

    def _project_manuscript(self, row: sqlite3.Row, project_id: int, user_id: int) -> dict[str, Any]:
        claim_ids = json.loads(row["claim_ids_json"])
        claims: list[dict[str, Any]] = []
        if claim_ids:
            marks = ",".join("?" for _ in claim_ids)
            claims = [dict(item) for item in self.db.execute(f"SELECT id,claim_code,title,status FROM claims WHERE id IN ({marks}) ORDER BY claim_code", tuple(claim_ids)).fetchall()]
        reviews: list[dict[str, Any]] = []
        cycle_info = None
        if row["review_cycle_id"]:
            cycle = self.db.execute("SELECT * FROM review_cycles WHERE id=?", (row["review_cycle_id"],)).fetchone()
            cycle_info = {"id": cycle["id"], "quorum": cycle["quorum_snapshot"], "eligible_roles": json.loads(cycle["eligible_roles_json"]), "open": bool(cycle["open_for_reviews"])}
            reviews = [
                {"reviewer_id": item["reviewer_id"], "decision": item["decision"], "comment": item["comment"], "created_at": item["created_at"]}
                for item in self.db.execute("SELECT * FROM manuscript_reviews WHERE cycle_id=? ORDER BY id", (cycle["id"],)).fetchall()
            ]
        events = [
            {"action": item["action"], "from_status": item["from_status"], "to_status": item["to_status"], "note": item["note"], "actor_id": item["actor_id"], "created_at": item["created_at"]}
            for item in self.db.execute("SELECT * FROM manuscript_events WHERE manuscript_id=? ORDER BY id", (row["id"],)).fetchall()
        ]
        return {
            "id": row["id"],
            "project_id": project_id,
            "title": row["title"],
            "status": row["status"],
            "author_id": row["author_id"],
            "version": row["version"],
            "claims": claims,
            "review_cycle": cycle_info,
            "reviews": reviews,
            "events": events,
            "submitted_at": row["submitted_at"],
            "published_at": row["published_at"],
            "withdrawn_at": row["withdrawn_at"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_publication(self, project_id: int, user_id: int, manuscript_id: int) -> dict[str, Any]:
        self.require_role(project_id, user_id, READ_ROLES)
        row = self._get_manuscript(project_id, manuscript_id)
        publication = self.db.execute("SELECT * FROM publications WHERE manuscript_id=?", (manuscript_id,)).fetchone()
        if publication is None:
            raise ServiceError("not_published", "稿件尚未发布", 404)
        privileged = self.evidence.can_view_restricted(project_id, user_id)
        package = json.loads(publication["package_json"])
        manifest = json.loads(publication["manifest_json"])
        projected_package = project_package(package, privileged)
        projected_manifest = project_manifest(manifest, projected_package, privileged)
        return {
            "manuscript_id": manuscript_id,
            "manuscript_status": row["status"],
            "published_at": publication["published_at"],
            "manifest_hash": publication["manifest_hash"],
            "projection": "full" if privileged else "redacted",
            "package": projected_package,
            "manifest": projected_manifest,
        }
