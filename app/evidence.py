from __future__ import annotations

import json
import sqlite3
from collections import deque
from typing import Any

from app.database import now, transaction
from app.security import content_hash, request_hash, stable_json
from app.service import BaseService, ServiceError

# 受限资源仅这些角色可见，其余角色只能看到安全投影（占位、不含内容）。
RESTRICTED_CLEARANCE = {"owner", "researcher"}
RESOURCE_WRITERS = {"owner", "researcher", "recorder"}
CLAIM_WRITERS = {"owner", "researcher"}
READERS = {"owner", "researcher", "recorder", "reviewer", "viewer"}


def json_diff(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    """递归比较两个 JSON 结构，输出稳定的字段级变更列表。"""
    changes: list[dict[str, Any]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            child = f"{path}/{key}"
            if key not in old:
                changes.append({"op": "add", "path": child, "from": None, "to": new[key]})
            elif key not in new:
                changes.append({"op": "remove", "path": child, "from": old[key], "to": None})
            else:
                changes.extend(json_diff(old[key], new[key], child))
    elif isinstance(old, list) and isinstance(new, list):
        for index in range(max(len(old), len(new))):
            child = f"{path}/{index}"
            if index >= len(old):
                changes.append({"op": "add", "path": child, "from": None, "to": new[index]})
            elif index >= len(new):
                changes.append({"op": "remove", "path": child, "from": old[index], "to": None})
            else:
                changes.extend(json_diff(old[index], new[index], child))
    elif old != new:
        changes.append({"op": "change", "path": path or "/", "from": old, "to": new})
    return changes


class EvidenceService(BaseService):
    """资源版本、论点、论据边与失效传播。所有变更在即时事务内完成并写入审计链。"""

    # ---------- 通用读取 ----------

    def _resource(self, resource_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            raise ServiceError("resource_not_found", "资源不存在", 404)
        return row

    def _claim(self, claim_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None:
            raise ServiceError("claim_not_found", "论点不存在", 404)
        return row

    def _edge(self, edge_id: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM claim_edges WHERE id=?", (edge_id,)).fetchone()
        if row is None:
            raise ServiceError("edge_not_found", "论据边不存在", 404)
        return row

    # ---------- 安全投影 ----------

    def _visible(self, resource: sqlite3.Row, role: str) -> bool:
        return resource["visibility"] == "project" or role in RESTRICTED_CLEARANCE

    def project_resource(self, resource: sqlite3.Row, role: str) -> dict[str, Any]:
        base = {
            "id": resource["id"],
            "project_id": resource["project_id"],
            "kind": resource["kind"],
            "code": resource["code"],
            "visibility": resource["visibility"],
            "status": resource["status"],
            "current_version": resource["current_version"],
            "created_at": resource["created_at"],
            "updated_at": resource["updated_at"],
            "withdrawn_at": resource["withdrawn_at"],
        }
        if self._visible(resource, role):
            base.update({"title": resource["title"], "created_by": resource["created_by"], "redacted": False})
        else:
            base.update({"title": "[已屏蔽]", "created_by": None, "redacted": True})
        return base

    def project_version(self, resource: sqlite3.Row, version: sqlite3.Row, role: str) -> dict[str, Any]:
        base = {"id": version["id"], "resource_id": version["resource_id"], "version": version["version"], "created_at": version["created_at"]}
        if self._visible(resource, role):
            base.update({"data": json.loads(version["data_json"]), "content_hash": version["content_hash"], "created_by": version["created_by"], "redacted": False})
        else:
            base.update({"data": None, "content_hash": None, "created_by": None, "redacted": True})
        return base

    # ---------- 资源 ----------

    def create_resource(self, project_id: int, payload: dict[str, Any], actor_id: int, key: str = "") -> dict[str, Any]:
        self.require_role(project_id, actor_id, RESOURCE_WRITERS)
        digest = request_hash({"project_id": project_id, **payload})
        if key:
            old = self.db.execute("SELECT * FROM idempotency_records WHERE scope='resource.create' AND request_key=?", (key,)).fetchone()
            if old:
                if old["request_hash"] != digest:
                    raise ServiceError("idempotency_conflict", "幂等键对应的请求内容不同", 409)
                return json.loads(old["response_json"])
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO resources(project_id,kind,code,title,visibility,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (project_id, payload["kind"], payload["code"], payload["title"], payload.get("visibility", "project"), actor_id, stamp, stamp),
                )
                resource_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO resource_versions(resource_id,version,data_json,content_hash,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (resource_id, 1, stable_json(payload.get("data", {})), content_hash(payload.get("data", {})), actor_id, stamp),
                )
                self.audit("resource.create", "resource", str(resource_id), payload, project_id=project_id, actor_id=actor_id)
                result = self.project_resource(self._resource(resource_id), "owner")
                result["versions"] = [self.project_version(self._resource(resource_id), self._version(resource_id, 1), "owner")]
                if key:
                    db.execute("INSERT INTO idempotency_records(scope,request_key,request_hash,response_json,created_at) VALUES('resource.create',?,?,?,?)", (key, digest, stable_json(result), stamp))
                return result
        except sqlite3.IntegrityError as exc:
            raise ServiceError("resource_exists", "项目内资源编码已存在", 409) from exc

    def _version(self, resource_id: int, version: int) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM resource_versions WHERE resource_id=? AND version=?", (resource_id, version)).fetchone()
        if row is None:
            raise ServiceError("version_not_found", f"资源版本 v{version} 不存在", 404)
        return row

    def list_resources(self, project_id: int, actor_id: int) -> list[dict[str, Any]]:
        role = self.require_role(project_id, actor_id, READERS)
        rows = self.db.execute("SELECT * FROM resources WHERE project_id=? ORDER BY code,id", (project_id,)).fetchall()
        return [self.project_resource(row, role) for row in rows]

    def get_resource(self, resource_id: int, actor_id: int) -> dict[str, Any]:
        resource = self._resource(resource_id)
        role = self.require_role(resource["project_id"], actor_id, READERS)
        result = self.project_resource(resource, role)
        versions = self.db.execute("SELECT * FROM resource_versions WHERE resource_id=? ORDER BY version", (resource_id,)).fetchall()
        result["versions"] = [self.project_version(resource, row, role) for row in versions]
        return result

    def add_version(self, resource_id: int, data: dict[str, Any], actor_id: int) -> dict[str, Any]:
        resource = self._resource(resource_id)
        self.require_role(resource["project_id"], actor_id, RESOURCE_WRITERS)
        if resource["status"] == "withdrawn":
            raise ServiceError("resource_withdrawn", "资源已撤回，不能追加版本", 409)
        with transaction(immediate=True) as db:
            version = resource["current_version"] + 1
            stamp = now()
            db.execute(
                "INSERT INTO resource_versions(resource_id,version,data_json,content_hash,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (resource_id, version, stable_json(data), content_hash(data), actor_id, stamp),
            )
            db.execute("UPDATE resources SET current_version=?,updated_at=? WHERE id=?", (version, stamp, resource_id))
            self.audit("resource.supersede", "resource", str(resource_id), {"version": version, "previous_version": resource["current_version"]}, project_id=resource["project_id"], actor_id=actor_id)
            changed = self._propagate_resource_change(resource_id)
            self.audit("claim.invalidation", "resource", str(resource_id), {"affected_claims": changed, "cause": "resource_superseded"}, project_id=resource["project_id"], actor_id=actor_id)
        return self.get_resource(resource_id, actor_id)

    def withdraw_resource(self, resource_id: int, actor_id: int) -> dict[str, Any]:
        resource = self._resource(resource_id)
        self.require_role(resource["project_id"], actor_id, RESOURCE_WRITERS)
        with transaction(immediate=True) as db:
            if resource["status"] != "withdrawn":
                db.execute("UPDATE resources SET status='withdrawn',withdrawn_at=?,updated_at=? WHERE id=?", (now(), now(), resource_id))
                self.audit("resource.withdraw", "resource", str(resource_id), {"code": resource["code"]}, project_id=resource["project_id"], actor_id=actor_id)
                changed = self._propagate_resource_change(resource_id)
                self.audit("claim.invalidation", "resource", str(resource_id), {"affected_claims": changed, "cause": "resource_withdrawn"}, project_id=resource["project_id"], actor_id=actor_id)
        return self.get_resource(resource_id, actor_id)

    def diff_versions(self, resource_id: int, from_version: int, to_version: int, actor_id: int) -> dict[str, Any]:
        resource = self._resource(resource_id)
        role = self.require_role(resource["project_id"], actor_id, READERS)
        old = self._version(resource_id, from_version)
        new = self._version(resource_id, to_version)
        result = {
            "resource_id": resource_id,
            "code": resource["code"],
            "from_version": from_version,
            "to_version": to_version,
            "from_hash": old["content_hash"],
            "to_hash": new["content_hash"],
        }
        if not self._visible(resource, role):
            # 受限资源对无权限角色只投影出版本号与哈希是否存在，不输出内容级 diff。
            result.update({"redacted": True, "changes": None, "from_hash": None, "to_hash": None})
            return result
        result.update({"redacted": False, "changes": json_diff(json.loads(old["data_json"]), json.loads(new["data_json"]))})
        return result

    # ---------- 论点 ----------

    def create_claim(self, project_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        self.require_role(project_id, actor_id, CLAIM_WRITERS)
        stamp = now()
        try:
            with transaction(immediate=True) as db:
                cursor = db.execute(
                    "INSERT INTO claims(project_id,code,title,statement,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (project_id, payload["code"], payload["title"], payload["statement"], actor_id, stamp, stamp),
                )
                claim_id = cursor.lastrowid
                db.execute(
                    "INSERT INTO claim_revisions(claim_id,revision,title,statement,reason,changed_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (claim_id, 1, payload["title"], payload["statement"], "初始版本", actor_id, stamp),
                )
                self.audit("claim.create", "claim", str(claim_id), payload, project_id=project_id, actor_id=actor_id)
                return self._claim_payload(self._claim(claim_id))
        except sqlite3.IntegrityError as exc:
            raise ServiceError("claim_exists", "项目内论点编号已存在", 409) from exc

    def _claim_payload(self, claim: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": claim["id"],
            "project_id": claim["project_id"],
            "code": claim["code"],
            "title": claim["title"],
            "statement": claim["statement"],
            "revision": claim["revision"],
            "affected": bool(claim["affected"]),
            "affected_reason": claim["affected_reason"],
            "status": claim["status"],
            "created_by": claim["created_by"],
            "created_at": claim["created_at"],
            "updated_at": claim["updated_at"],
        }

    def list_claims(self, project_id: int, actor_id: int, affected: bool | None = None) -> list[dict[str, Any]]:
        self.require_role(project_id, actor_id, READERS)
        query = "SELECT * FROM claims WHERE project_id=?"
        params: list[Any] = [project_id]
        if affected is not None:
            query += " AND affected=?"
            params.append(1 if affected else 0)
        rows = self.db.execute(query + " ORDER BY code,id", params).fetchall()
        return [self._claim_payload(row) for row in rows]

    def get_claim(self, claim_id: int, actor_id: int) -> dict[str, Any]:
        claim = self._claim(claim_id)
        role = self.require_role(claim["project_id"], actor_id, READERS)
        result = self._claim_payload(claim)
        edges = self.db.execute("SELECT * FROM claim_edges WHERE from_claim_id=? ORDER BY id", (claim_id,)).fetchall()
        result["edges"] = [self._edge_payload(row, role) for row in edges]
        cited_by = self.db.execute("SELECT * FROM claim_edges WHERE target_claim_id=? AND status='active' ORDER BY id", (claim_id,)).fetchall()
        result["cited_by"] = [self._edge_payload(row, role) for row in cited_by]
        impacts = self.db.execute("SELECT * FROM claim_impacts WHERE claim_id=? ORDER BY id", (claim_id,)).fetchall()
        result["impacts"] = [dict(row) for row in impacts]
        return result

    def _edge_payload(self, edge: sqlite3.Row, role: str) -> dict[str, Any]:
        payload = {
            "id": edge["id"],
            "from_claim_id": edge["from_claim_id"],
            "edge_type": edge["edge_type"],
            "status": edge["status"],
            "created_by": edge["created_by"],
            "created_at": edge["created_at"],
            "withdrawn_at": edge["withdrawn_at"],
        }
        if edge["target_claim_id"] is not None:
            target = self._claim(edge["target_claim_id"])
            payload["target"] = {"type": "claim", "claim_id": target["id"], "code": target["code"], "title": target["title"], "affected": bool(target["affected"])}
        else:
            resource = self._resource(edge["target_resource_id"])
            target: dict[str, Any] = {
                "type": "resource",
                "resource_id": resource["id"],
                "code": resource["code"],
                "kind": resource["kind"],
                "pinned_version": edge["target_resource_version"],
                "current_version": resource["current_version"],
                "resource_status": resource["status"],
                "stale": resource["status"] == "withdrawn" or edge["target_resource_version"] != resource["current_version"],
            }
            if self._visible(resource, role):
                target["title"] = resource["title"]
                target["redacted"] = False
            else:
                target["title"] = "[已屏蔽]"
                target["redacted"] = True
            payload["target"] = target
        return payload

    def update_claim(self, claim_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        claim = self._claim(claim_id)
        self.require_role(claim["project_id"], actor_id, CLAIM_WRITERS)
        title = payload.get("title") or claim["title"]
        statement = payload.get("statement") or claim["statement"]
        reason = payload.get("reason", "")
        if title == claim["title"] and statement == claim["statement"]:
            raise ServiceError("no_change", "论点内容没有变化", 409)
        with transaction(immediate=True) as db:
            revision = claim["revision"] + 1
            stamp = now()
            db.execute("UPDATE claims SET title=?,statement=?,revision=?,updated_at=? WHERE id=?", (title, statement, revision, stamp, claim_id))
            db.execute(
                "INSERT INTO claim_revisions(claim_id,revision,title,statement,reason,changed_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (claim_id, revision, title, statement, reason, actor_id, stamp),
            )
            self.audit("claim.revise", "claim", str(claim_id), {"revision": revision, "reason": reason, "title": title, "statement": statement}, project_id=claim["project_id"], actor_id=actor_id)
        return self.get_claim(claim_id, actor_id)

    def claim_revisions(self, claim_id: int, actor_id: int) -> list[dict[str, Any]]:
        claim = self._claim(claim_id)
        self.require_role(claim["project_id"], actor_id, READERS)
        rows = self.db.execute("SELECT * FROM claim_revisions WHERE claim_id=? ORDER BY revision", (claim_id,)).fetchall()
        return [dict(row) for row in rows]

    # ---------- 论据边 ----------

    def _find_claim_path(self, start: int, goal: int) -> list[int] | None:
        """沿活跃论点边从 start 找一条到 goal 的依赖路径（用于环路报告）。"""
        stack = [(start, [start])]
        visited = {start}
        while stack:
            node, path = stack.pop()
            if node == goal:
                return path
            rows = self.db.execute("SELECT target_claim_id FROM claim_edges WHERE from_claim_id=? AND target_claim_id IS NOT NULL AND status='active'", (node,)).fetchall()
            for row in rows:
                nxt = row["target_claim_id"]
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append((nxt, path + [nxt]))
        return None

    def create_edge(self, claim_id: int, payload: dict[str, Any], actor_id: int) -> dict[str, Any]:
        claim = self._claim(claim_id)
        self.require_role(claim["project_id"], actor_id, CLAIM_WRITERS)
        target_claim_id = payload.get("target_claim_id")
        target_resource_id = payload.get("target_resource_id")
        if (target_claim_id is None) == (target_resource_id is None):
            raise ServiceError("invalid_target", "论据边必须且只能指向一个论点或一个资源", 400)
        with transaction(immediate=True) as db:
            pinned_version: int | None = None
            if target_claim_id is not None:
                target = self._claim(target_claim_id)
                if target["project_id"] != claim["project_id"]:
                    raise ServiceError("cross_project", "不能跨项目连接论点", 400)
                existing_path = self._find_claim_path(target_claim_id, claim_id)
                if existing_path is not None:
                    cycle = [claim_id] + existing_path
                    codes = [self._claim(node)["code"] for node in cycle]
                    raise ServiceError("cycle_detected", "论点依赖成环，已拒绝该边", 409, {"path": cycle, "path_codes": codes})
                duplicate = db.execute(
                    "SELECT id FROM claim_edges WHERE from_claim_id=? AND edge_type=? AND target_claim_id=? AND status='active'",
                    (claim_id, payload["edge_type"], target_claim_id),
                ).fetchone()
            else:
                resource = self._resource(target_resource_id)
                if resource["project_id"] != claim["project_id"]:
                    raise ServiceError("cross_project", "不能引用其他项目的资源", 400)
                if resource["status"] == "withdrawn":
                    raise ServiceError("resource_withdrawn", "资源已撤回，不能新增引用", 409)
                requested = payload.get("resource_version")
                if requested is None:
                    pinned_version = resource["current_version"]
                else:
                    self._version(target_resource_id, requested)
                    pinned_version = requested
                duplicate = db.execute(
                    "SELECT id FROM claim_edges WHERE from_claim_id=? AND edge_type=? AND target_resource_id=? AND target_resource_version=? AND status='active'",
                    (claim_id, payload["edge_type"], target_resource_id, pinned_version),
                ).fetchone()
            if duplicate:
                raise ServiceError("edge_exists", "相同的论据边已存在", 409)
            stamp = now()
            cursor = db.execute(
                "INSERT INTO claim_edges(project_id,from_claim_id,edge_type,target_claim_id,target_resource_id,target_resource_version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (claim["project_id"], claim_id, payload["edge_type"], target_claim_id, target_resource_id, pinned_version, actor_id, stamp),
            )
            edge_id = cursor.lastrowid
            self.audit("edge.create", "edge", str(edge_id), {"from_claim_id": claim_id, **payload, "pinned_version": pinned_version}, project_id=claim["project_id"], actor_id=actor_id)
            self._recompute_and_propagate([claim_id])
            edge = self._edge(edge_id)
        role = self.member_role(claim["project_id"], actor_id) or "viewer"
        return self._edge_payload(edge, role)

    def withdraw_edge(self, edge_id: int, actor_id: int) -> dict[str, Any]:
        edge = self._edge(edge_id)
        self.require_role(edge["project_id"], actor_id, CLAIM_WRITERS)
        with transaction(immediate=True) as db:
            if edge["status"] == "withdrawn":
                raise ServiceError("edge_withdrawn", "论据边已撤回", 409)
            db.execute("UPDATE claim_edges SET status='withdrawn',withdrawn_at=? WHERE id=?", (now(), edge_id))
            self.audit("edge.withdraw", "edge", str(edge_id), {"from_claim_id": edge["from_claim_id"]}, project_id=edge["project_id"], actor_id=actor_id)
            self._recompute_and_propagate([edge["from_claim_id"]])
        role = self.member_role(edge["project_id"], actor_id) or "viewer"
        return self._edge_payload(self._edge(edge_id), role)

    def repin_edge(self, edge_id: int, version: int, actor_id: int) -> dict[str, Any]:
        edge = self._edge(edge_id)
        self.require_role(edge["project_id"], actor_id, CLAIM_WRITERS)
        if edge["target_resource_id"] is None:
            raise ServiceError("not_resource_edge", "只有资源引用边可以重新钉版", 400)
        if edge["status"] != "active":
            raise ServiceError("edge_withdrawn", "论据边已撤回，不能重新钉版", 409)
        resource = self._resource(edge["target_resource_id"])
        if resource["status"] == "withdrawn":
            raise ServiceError("resource_withdrawn", "资源已撤回，不能重新钉版", 409)
        self._version(edge["target_resource_id"], version)
        with transaction(immediate=True) as db:
            db.execute("UPDATE claim_edges SET target_resource_version=? WHERE id=?", (version, edge_id))
            self.audit("edge.repin", "edge", str(edge_id), {"resource_id": edge["target_resource_id"], "version": version}, project_id=edge["project_id"], actor_id=actor_id)
            self._recompute_and_propagate([edge["from_claim_id"]])
        role = self.member_role(edge["project_id"], actor_id) or "viewer"
        return self._edge_payload(self._edge(edge_id), role)

    # ---------- 失效传播 ----------

    def _compute_affected(self, claim_id: int) -> tuple[bool, str]:
        reasons: list[str] = []
        edges = self.db.execute("SELECT * FROM claim_edges WHERE from_claim_id=? AND status='active'", (claim_id,)).fetchall()
        for edge in edges:
            if edge["target_resource_id"] is not None:
                resource = self._resource(edge["target_resource_id"])
                if resource["status"] == "withdrawn":
                    reasons.append(f"资源 {resource['code']} 已撤回")
                elif edge["target_resource_version"] != resource["current_version"]:
                    reasons.append(f"资源 {resource['code']} 引用版本 v{edge['target_resource_version']} 已被 v{resource['current_version']} 替换")
            else:
                target = self._claim(edge["target_claim_id"])
                if target["affected"]:
                    reasons.append(f"依赖论点 {target['code']} 受影响")
        return (bool(reasons), "；".join(reasons))

    def _recompute_and_propagate(self, seed_claim_ids: list[int]) -> list[int]:
        """从种子论点出发重算受影响标记，并沿依赖图（被引用方→引用方）传播。历史论点只标记、不删除。"""
        changed: list[int] = []
        queue: deque[int] = deque(seed_claim_ids)
        while queue:
            claim_id = queue.popleft()
            row = self._claim(claim_id)
            affected, reason = self._compute_affected(claim_id)
            if affected == bool(row["affected"]) and reason == row["affected_reason"]:
                continue
            stamp = now()
            self.db.execute("UPDATE claims SET affected=?,affected_reason=?,updated_at=? WHERE id=?", (1 if affected else 0, reason, stamp, claim_id))
            self.db.execute(
                "INSERT INTO claim_impacts(claim_id,cause,source_edge_id,detail,created_at) VALUES(?,?,?,?,?)",
                (claim_id, "invalidated" if affected else "resolved", None, reason or "引用恢复有效", stamp),
            )
            changed.append(claim_id)
            dependents = self.db.execute("SELECT from_claim_id FROM claim_edges WHERE target_claim_id=? AND status='active'", (claim_id,)).fetchall()
            for dependent in dependents:
                queue.append(dependent["from_claim_id"])
        return changed

    def _propagate_resource_change(self, resource_id: int) -> list[int]:
        rows = self.db.execute("SELECT DISTINCT from_claim_id FROM claim_edges WHERE target_resource_id=? AND status='active'", (resource_id,)).fetchall()
        return self._recompute_and_propagate([row["from_claim_id"] for row in rows])

    def resolve_claim(self, claim_id: int, actor_id: int) -> dict[str, Any]:
        claim = self._claim(claim_id)
        self.require_role(claim["project_id"], actor_id, CLAIM_WRITERS)
        with transaction(immediate=True) as db:
            changed = self._recompute_and_propagate([claim_id])
            self.audit("claim.resolve", "claim", str(claim_id), {"recomputed": changed}, project_id=claim["project_id"], actor_id=actor_id)
        return self.get_claim(claim_id, actor_id)
