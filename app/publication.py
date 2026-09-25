from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import now, transaction
from app.evidence import READERS, RESTRICTED_CLEARANCE, EvidenceService
from app.security import content_hash
from app.service import BaseService, ServiceError

ARGUMENT_AUTHORS = {"owner", "researcher"}
PUBLISHERS = {"owner"}
PACKAGE_FORMAT = "argument-publication/1"
PACKAGE_SECTIONS = ("argument", "policy_snapshot", "approvals", "claims", "evidence", "objections", "warnings")
DEFAULT_POLICY = {"policy_id": None, "name": "默认评审策略", "quorum": 1, "reviewer_roles": ["owner", "reviewer"]}


def package_content_hash(package: dict[str, Any]) -> str:
    """发布包内容哈希：覆盖除 content_hash 与 manifest 外的全部字段。"""
    core = {key: value for key, value in package.items() if key not in ("content_hash", "manifest")}
    return content_hash(core)


def build_manifest(package: dict[str, Any]) -> dict[str, Any]:
    sections = {name: content_hash(package.get(name)) for name in PACKAGE_SECTIONS}
    return {"algorithm": "sha256", "format": PACKAGE_FORMAT, "sections": sections, "content_hash": package_content_hash(package)}


def verify_package(package: dict[str, Any]) -> dict[str, Any]:
    """离线校验发布包：逐节重算哈希并与清单、内容哈希比对。"""
    checks: list[dict[str, Any]] = []
    valid = True
    manifest = package.get("manifest") or {}
    for name, expected in sorted((manifest.get("sections") or {}).items()):
        actual = content_hash(package.get(name))
        ok = actual == expected
        valid = valid and ok
        checks.append({"section": name, "expected": expected, "actual": actual, "ok": ok})
    recomputed = package_content_hash(package)
    content_ok = recomputed == package.get("content_hash") and recomputed == manifest.get("content_hash")
    valid = valid and content_ok
    checks.append({"section": "*content_hash*", "expected": package.get("content_hash"), "actual": recomputed, "ok": content_ok})
    return {"valid": valid, "format": package.get("format"), "checks": checks}


class PublicationService(BaseService):
    def __init__(self, db: sqlite3.Connection | None = None):
        super().__init__(db)
        self.evidence = EvidenceService(self.db)

    # ---------- 评审策略 ----------

    def create_policy(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, {"owner"})
        with transaction(immediate=True) as db:
            db.execute("UPDATE review_policies SET active=0 WHERE project_id=?", (project_id,))
            cursor = db.execute(
                "INSERT INTO review_policies(project_id,name,quorum,reviewer_roles,active,created_by,created_at) VALUES(?,?,?,?,1,?,?)",
                (project_id, payload["name"], payload["quorum"], json.dumps(payload["reviewer_roles"], ensure_ascii=False), actor_id, now()),
            )
            self.audit("policy.set", "review_policy", str(cursor.lastrowid), payload, project_id=project_id, actor_id=actor_id)
            row = db.execute("SELECT * FROM review_policies WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._policy_payload(row)

    def _policy_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "project_id": row["project_id"], "name": row["name"], "quorum": row["quorum"], "reviewer_roles": json.loads(row["reviewer_roles"]), "active": bool(row["active"]), "created_by": row["created_by"], "created_at": row["created_at"]}

    def list_policies(self, project_id: int, actor_id: int) -> list[dict[str, Any]]:
        self.require_role(project_id, actor_id, READERS)
        rows = self.db.execute("SELECT * FROM review_policies WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._policy_payload(row) for row in rows]

    def _active_policy_snapshot(self, project_id: int) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM review_policies WHERE project_id=? AND active=1 ORDER BY id DESC LIMIT 1", (project_id,)).fetchone()
        if row is None:
            snapshot = dict(DEFAULT_POLICY)
        else:
            snapshot = {"policy_id": row["id"], "name": row["name"], "quorum": row["quorum"], "reviewer_roles": json.loads(row["reviewer_roles"])}
        snapshot["captured_at"] = now()
        return snapshot

    # ---------- 论证稿 ----------

    def _argument(self, argument_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM arguments WHERE id=?", (argument_id,)).fetchone()
        if row is None:
            raise ServiceError("argument_not_found", "论证稿不存在", 404)
        return row

    def _revision(self, argument_id: int, revision: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM argument_revisions WHERE argument_id=? AND revision=?", (argument_id, revision)).fetchone()
        if row is None:
            raise ServiceError("revision_not_found", f"论证稿修订版 r{revision} 不存在", 404)
        return row

    def _validate_claim_ids(self, project_id: int, claim_ids: list[int]) -> None:
        for claim_id in claim_ids:
            claim = self.evidence._claim(claim_id)
            if claim["project_id"] != project_id:
                raise ServiceError("cross_project", "论证稿只能引用本项目论点", 400)

    def create_argument(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, ARGUMENT_AUTHORS)
        self._validate_claim_ids(project_id, payload.get("claim_ids", []))
        stamp = now()
        with transaction(immediate=True) as db:
            cursor = db.execute(
                "INSERT INTO arguments(project_id,title,author_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                (project_id, payload["title"], actor_id, stamp, stamp),
            )
            argument_id = cursor.lastrowid
            db.execute(
                "INSERT INTO argument_revisions(argument_id,revision,claim_ids_json,summary,changed_by,created_at) VALUES(?,?,?,?,?,?)",
                (argument_id, 1, json.dumps(payload.get("claim_ids", [])), payload.get("summary", ""), actor_id, stamp),
            )
            self.audit("argument.create", "argument", str(argument_id), payload, project_id=project_id, actor_id=actor_id)
        return self.get_argument(argument_id, actor_id)

    def list_arguments(self, project_id: int, actor_id: int) -> list[dict[str, Any]]:
        self.require_role(project_id, actor_id, READERS)
        rows = self.db.execute("SELECT * FROM arguments WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
        return [self._argument_summary(row) for row in rows]

    def _argument_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "project_id": row["project_id"], "title": row["title"], "status": row["status"], "author_id": row["author_id"], "current_revision": row["current_revision"], "submitted_at": row["submitted_at"], "published_at": row["published_at"], "retracted_at": row["retracted_at"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get_argument(self, argument_id: int, actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        self.require_role(argument["project_id"], actor_id, READERS)
        result = self._argument_summary(argument)
        revision = self._revision(argument_id, argument["current_revision"])
        result["summary"] = revision["summary"]
        result["claim_ids"] = json.loads(revision["claim_ids_json"])
        snapshot = json.loads(argument["policy_snapshot_json"]) if argument["policy_snapshot_json"] else None
        result["policy_snapshot"] = snapshot
        reviews = self.db.execute("SELECT * FROM reviews WHERE argument_id=? ORDER BY id", (argument_id,)).fetchall()
        result["reviews"] = [dict(row) for row in reviews]
        if snapshot:
            result["approvals"] = {"count": self._approval_count(argument, snapshot), "quorum": snapshot["quorum"]}
        else:
            result["approvals"] = None
        publication = self.db.execute("SELECT id,revision,content_hash,created_at FROM publications WHERE argument_id=? ORDER BY revision DESC LIMIT 1", (argument_id,)).fetchone()
        result["publication"] = dict(publication) if publication else None
        return result

    def update_argument(self, argument_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        role = self.require_role(argument["project_id"], actor_id, ARGUMENT_AUTHORS)
        if argument["author_id"] != actor_id and role != "owner":
            raise ServiceError("forbidden", "只有作者或项目负责人可以修改论证稿", 403)
        if argument["status"] not in ("draft", "changes_requested"):
            raise ServiceError("invalid_state", f"当前状态 {argument['status']} 不允许修改内容", 409)
        current = self._revision(argument_id, argument["current_revision"])
        claim_ids = payload.get("claim_ids")
        if claim_ids is None:
            claim_ids = json.loads(current["claim_ids_json"])
        self._validate_claim_ids(argument["project_id"], claim_ids)
        summary = payload.get("summary")
        if summary is None:
            summary = current["summary"]
        title = payload.get("title") or argument["title"]
        with transaction(immediate=True) as db:
            revision_no = argument["current_revision"] + 1
            stamp = now()
            updated = db.execute("UPDATE arguments SET title=?,current_revision=?,updated_at=? WHERE id=? AND status IN ('draft','changes_requested')", (title, revision_no, stamp, argument_id)).rowcount
            if not updated:
                raise ServiceError("invalid_state", "论证稿状态已变化，请刷新后重试", 409)
            try:
                db.execute(
                    "INSERT INTO argument_revisions(argument_id,revision,claim_ids_json,summary,changed_by,created_at) VALUES(?,?,?,?,?,?)",
                    (argument_id, revision_no, json.dumps(claim_ids), summary, actor_id, stamp),
                )
            except sqlite3.IntegrityError as exc:
                raise ServiceError("concurrent_revision", "修订版号冲突，请刷新后重试", 409) from exc
            self.audit("argument.revise", "argument", str(argument_id), {"revision": revision_no, "claim_ids": claim_ids, "summary": summary, "title": title}, project_id=argument["project_id"], actor_id=actor_id)
        return self.get_argument(argument_id, actor_id)

    def submit(self, argument_id: int, actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        role = self.require_role(argument["project_id"], actor_id, ARGUMENT_AUTHORS)
        if argument["author_id"] != actor_id and role != "owner":
            raise ServiceError("forbidden", "只有作者或项目负责人可以送审", 403)
        if argument["status"] not in ("draft", "changes_requested"):
            raise ServiceError("invalid_state", f"当前状态 {argument['status']} 不允许送审", 409)
        with transaction(immediate=True) as db:
            snapshot = self._active_policy_snapshot(argument["project_id"])
            stamp = now()
            updated = db.execute("UPDATE arguments SET status='submitted',policy_snapshot_json=?,submitted_at=?,updated_at=? WHERE id=? AND status IN ('draft','changes_requested')", (json.dumps(snapshot, ensure_ascii=False, sort_keys=True), stamp, stamp, argument_id)).rowcount
            if not updated:
                raise ServiceError("invalid_state", "论证稿状态已变化，请刷新后重试", 409)
            self.audit("argument.submit", "argument", str(argument_id), {"revision": argument["current_revision"], "policy_snapshot": snapshot}, project_id=argument["project_id"], actor_id=actor_id)
        return self.get_argument(argument_id, actor_id)

    # ---------- 评审 ----------

    def _approval_count(self, argument: sqlite3.Row, snapshot: dict[str, Any]) -> int:
        roles = tuple(snapshot["reviewer_roles"])
        placeholders = ",".join("?" for _ in roles)
        row = self.db.execute(
            f"SELECT COUNT(DISTINCT r.reviewer_id) AS n FROM reviews r JOIN project_members m ON m.project_id=? AND m.user_id=r.reviewer_id "
            f"WHERE r.argument_id=? AND r.revision=? AND r.decision='approve' AND r.reviewer_id<>? AND m.role IN ({placeholders})",
            (argument["project_id"], argument["id"], argument["current_revision"], argument["author_id"], *roles),
        ).fetchone()
        return row["n"]

    def review(self, argument_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        if argument["status"] != "submitted":
            # 重复回调幂等：状态已推进时，同一评审人相同结论返回原记录，不产生第二条。
            existing = self.db.execute("SELECT * FROM reviews WHERE argument_id=? AND revision=? AND reviewer_id=?", (argument_id, argument["current_revision"], actor_id)).fetchone()
            if existing is not None and existing["decision"] == payload["decision"]:
                return {**dict(existing), "replayed": True, "argument_status": argument["status"]}
            raise ServiceError("invalid_state", f"当前状态 {argument['status']} 不允许评审", 409)
        snapshot = json.loads(argument["policy_snapshot_json"])
        role = self.require_role(argument["project_id"], actor_id, set(snapshot["reviewer_roles"]))
        if actor_id == argument["author_id"]:
            raise ServiceError("self_review", "作者不能评审自己的稿件", 403)
        stamp = now()
        with transaction(immediate=True) as db:
            try:
                cursor = db.execute(
                    "INSERT INTO reviews(argument_id,revision,reviewer_id,decision,comment,created_at) VALUES(?,?,?,?,?,?)",
                    (argument_id, argument["current_revision"], actor_id, payload["decision"], payload.get("comment", ""), stamp),
                )
                review_id = cursor.lastrowid
                replayed = False
            except sqlite3.IntegrityError:
                # 重复回调：同一评审人对同一修订版只能有一条结论；结论相同则幂等返回，不同则冲突。
                existing = db.execute("SELECT * FROM reviews WHERE argument_id=? AND revision=? AND reviewer_id=?", (argument_id, argument["current_revision"], actor_id)).fetchone()
                if existing["decision"] != payload["decision"]:
                    raise ServiceError("review_conflict", "同一评审人对同一修订版已有不同结论", 409)
                review_id = existing["id"]
                replayed = True
            self.audit("argument.review", "argument", str(argument_id), {"review_id": review_id, "decision": payload["decision"], "comment": payload.get("comment", ""), "replayed": replayed}, project_id=argument["project_id"], actor_id=actor_id)
            if replayed:
                row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
                return {**dict(row), "replayed": True, "argument_status": argument["status"]}
            if payload["decision"] == "request_changes":
                db.execute("UPDATE arguments SET status='changes_requested',updated_at=? WHERE id=? AND status='submitted'", (stamp, argument_id))
                self.audit("argument.changes_requested", "argument", str(argument_id), {"review_id": review_id}, project_id=argument["project_id"], actor_id=actor_id)
            else:
                fresh = self._argument(argument_id)
                if self._approval_count(fresh, snapshot) >= snapshot["quorum"]:
                    updated = db.execute("UPDATE arguments SET status='approved',updated_at=? WHERE id=? AND status='submitted'", (stamp, argument_id)).rowcount
                    if updated:
                        self.audit("argument.approved", "argument", str(argument_id), {"revision": argument["current_revision"], "quorum": snapshot["quorum"]}, project_id=argument["project_id"], actor_id=actor_id)
            row = db.execute("SELECT * FROM reviews WHERE id=?", (review_id,)).fetchone()
            status = db.execute("SELECT status FROM arguments WHERE id=?", (argument_id,)).fetchone()["status"]
            return {**dict(row), "replayed": False, "argument_status": status}

    # ---------- 发布 ----------

    def _build_package(self, argument: sqlite3.Row, actor_id: int) -> dict[str, Any]:
        revision = self._revision(argument["id"], argument["current_revision"])
        claim_ids = json.loads(revision["claim_ids_json"])
        claims = sorted((self.evidence._claim(cid) for cid in claim_ids), key=lambda row: (row["code"], row["id"]))
        claim_entries: list[dict[str, Any]] = []
        evidence_entries: list[dict[str, Any]] = []
        included = set(claim_ids)
        for claim in claims:
            claim_entries.append({"id": claim["id"], "code": claim["code"], "title": claim["title"], "statement": claim["statement"], "revision": claim["revision"], "affected": bool(claim["affected"]), "affected_reason": claim["affected_reason"]})
            edges = self.db.execute("SELECT * FROM claim_edges WHERE from_claim_id=? AND status='active' ORDER BY id", (claim["id"],)).fetchall()
            for edge in edges:
                if edge["target_resource_id"] is None:
                    continue
                resource = self.evidence._resource(edge["target_resource_id"])
                version = self.db.execute("SELECT * FROM resource_versions WHERE resource_id=? AND version=?", (resource["id"], edge["target_resource_version"])).fetchone()
                evidence_entries.append({
                    "claim_code": claim["code"],
                    "edge_id": edge["id"],
                    "edge_type": edge["edge_type"],
                    "resource_id": resource["id"],
                    "resource_code": resource["code"],
                    "kind": resource["kind"],
                    "title": resource["title"],
                    "pinned_version": edge["target_resource_version"],
                    "current_version": resource["current_version"],
                    "resource_status": resource["status"],
                    "version_hash": version["content_hash"] if version else None,
                })
        evidence_entries.sort(key=lambda item: (item["claim_code"], item["edge_id"]))
        rebut_rows = self.db.execute(
            "SELECT e.*, c.code AS from_code, t.code AS target_code FROM claim_edges e "
            "JOIN claims c ON c.id=e.from_claim_id JOIN claims t ON t.id=e.target_claim_id "
            "WHERE e.edge_type='rebuts' AND e.status='active' ORDER BY e.id"
        ).fetchall()
        rebut_edges = [{"edge_id": row["id"], "from_claim_code": row["from_code"], "target_claim_code": row["target_code"], "created_at": row["created_at"]} for row in rebut_rows if row["target_claim_id"] in included or row["from_claim_id"] in included]
        objection_rows = self.db.execute("SELECT * FROM reviews WHERE argument_id=? AND decision='request_changes' ORDER BY id", (argument["id"],)).fetchall()
        review_objections = [{"revision": row["revision"], "reviewer_id": row["reviewer_id"], "comment": row["comment"], "created_at": row["created_at"]} for row in objection_rows]
        approval_rows = self.db.execute("SELECT * FROM reviews WHERE argument_id=? AND revision=? AND decision='approve' ORDER BY reviewer_id", (argument["id"], argument["current_revision"])).fetchall()
        approvals = [{"reviewer_id": row["reviewer_id"], "created_at": row["created_at"]} for row in approval_rows]
        warnings = [f"论点 {claim['code']} 当前处于受影响状态" for claim in claims if claim["affected"]]
        package: dict[str, Any] = {
            "format": PACKAGE_FORMAT,
            "argument": {"id": argument["id"], "project_id": argument["project_id"], "title": argument["title"], "revision": argument["current_revision"], "status": "published"},
            "published_at": now(),
            "published_by": actor_id,
            "policy_snapshot": json.loads(argument["policy_snapshot_json"]),
            "approvals": approvals,
            "claims": claim_entries,
            "evidence": evidence_entries,
            "objections": {"rebut_edges": rebut_edges, "review_objections": review_objections},
            "warnings": warnings,
        }
        package["content_hash"] = package_content_hash(package)
        package["manifest"] = build_manifest(package)
        return package

    def publish(self, argument_id: int, actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        self.require_role(argument["project_id"], actor_id, PUBLISHERS)
        with transaction(immediate=True) as db:
            current = self._argument(argument_id)
            if current["status"] == "published":
                # 重复回调：发布是幂等的，直接返回既有发布包。
                existing = db.execute("SELECT * FROM publications WHERE argument_id=? AND revision=?", (argument_id, current["current_revision"])).fetchone()
                return {**self._publication_payload(existing), "replayed": True}
            if current["status"] != "approved":
                raise ServiceError("invalid_state", f"当前状态 {current['status']} 不允许发布", 409)
            snapshot = json.loads(current["policy_snapshot_json"])
            approvals = self._approval_count(current, snapshot)
            if approvals < snapshot["quorum"]:
                raise ServiceError("quorum_not_met", "批准数量未达到送审时的法定人数", 409)
            package = self._build_package(current, actor_id)
            stamp = now()
            db.execute(
                "INSERT INTO publications(argument_id,revision,package_json,content_hash,manifest_json,created_by,created_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(argument_id,revision) DO NOTHING",
                (argument_id, current["current_revision"], json.dumps(package, ensure_ascii=False, sort_keys=True), package["content_hash"], json.dumps(package["manifest"], ensure_ascii=False, sort_keys=True), actor_id, stamp),
            )
            db.execute("UPDATE arguments SET status='published',published_at=?,updated_at=? WHERE id=? AND status='approved'", (stamp, stamp, argument_id))
            self.audit("argument.publish", "argument", str(argument_id), {"revision": current["current_revision"], "content_hash": package["content_hash"]}, project_id=current["project_id"], actor_id=actor_id)
            row = db.execute("SELECT * FROM publications WHERE argument_id=? AND revision=?", (argument_id, current["current_revision"])).fetchone()
            return {**self._publication_payload(row), "replayed": False}

    def _publication_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {"id": row["id"], "argument_id": row["argument_id"], "revision": row["revision"], "content_hash": row["content_hash"], "manifest": json.loads(row["manifest_json"]), "package": json.loads(row["package_json"]), "created_by": row["created_by"], "created_at": row["created_at"]}

    def retract(self, argument_id: int, actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        self.require_role(argument["project_id"], actor_id, PUBLISHERS)
        with transaction(immediate=True) as db:
            if argument["status"] != "published":
                raise ServiceError("invalid_state", f"当前状态 {argument['status']} 不允许撤回", 409)
            db.execute("UPDATE arguments SET status='retracted',retracted_at=?,updated_at=? WHERE id=?", (now(), now(), argument_id))
            self.audit("argument.retract", "argument", str(argument_id), {"revision": argument["current_revision"]}, project_id=argument["project_id"], actor_id=actor_id)
        return self.get_argument(argument_id, actor_id)

    # ---------- 发布包读取与投影 ----------

    def get_publication(self, argument_id: int, actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        role = self.require_role(argument["project_id"], actor_id, READERS)
        row = self.db.execute("SELECT * FROM publications WHERE argument_id=? ORDER BY revision DESC LIMIT 1", (argument_id,)).fetchone()
        if row is None:
            raise ServiceError("publication_not_found", "该论证稿尚未发布", 404)
        payload = self._publication_payload(row)
        payload["package"] = self._project_package(payload["package"], argument["project_id"], role)
        return payload

    def _project_package(self, package: dict[str, Any], project_id: int, role: str) -> dict[str, Any]:
        """部分证据不可见时的安全投影：受限资源只保留标识与钉版信息，内容字段屏蔽。"""
        if role in RESTRICTED_CLEARANCE:
            return package
        projected = json.loads(json.dumps(package))
        redacted = False
        resource_cache: dict[int, sqlite3.Row] = {}
        for entry in projected.get("evidence", []):
            resource_id = entry["resource_id"]
            if resource_id not in resource_cache:
                resource_cache[resource_id] = self.evidence._resource(resource_id)
            resource = resource_cache[resource_id]
            if resource["visibility"] == "restricted":
                entry["title"] = "[已屏蔽]"
                entry["version_hash"] = None
                entry["redacted"] = True
                redacted = True
        if redacted:
            projected["projection"] = "redacted"
        return projected

    # ---------- 修订版比较 ----------

    def diff_revisions(self, argument_id: int, from_revision: int, to_revision: int, actor_id: int) -> dict[str, Any]:
        argument = self._argument(argument_id)
        self.require_role(argument["project_id"], actor_id, READERS)
        old = self._revision(argument_id, from_revision)
        new = self._revision(argument_id, to_revision)
        old_claims = set(json.loads(old["claim_ids_json"]))
        new_claims = set(json.loads(new["claim_ids_json"]))
        return {
            "argument_id": argument_id,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "claims_added": sorted(new_claims - old_claims),
            "claims_removed": sorted(old_claims - new_claims),
            "summary_changed": old["summary"] != new["summary"],
        }
