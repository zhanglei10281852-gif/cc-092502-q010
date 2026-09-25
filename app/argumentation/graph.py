"""论证依赖图：环路检测与证据失效影响传播。历史结论只标记、不删除。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.database import now
from app.security import stable_json


def load_claim_codes(db: sqlite3.Connection, claim_ids: set[int]) -> dict[int, str]:
    if not claim_ids:
        return {}
    marks = ",".join("?" for _ in claim_ids)
    rows = db.execute(f"SELECT id,claim_code FROM claims WHERE id IN ({marks})", tuple(claim_ids)).fetchall()
    return {row["id"]: row["claim_code"] for row in rows}


def find_cycle_path(db: sqlite3.Connection, source_id: int, target_id: int) -> list[str] | None:
    """若新增 source→target 的论点依赖会形成环，返回环路上的论点编码（含首尾呼应）。"""
    if source_id == target_id:
        codes = load_claim_codes(db, {source_id})
        return [codes[source_id], codes[source_id]]
    adjacency: dict[int, list[int]] = {}
    for row in db.execute("SELECT source_claim_id,target_claim_id FROM claim_edges WHERE target_claim_id IS NOT NULL"):
        adjacency.setdefault(row["source_claim_id"], []).append(row["target_claim_id"])
    parent: dict[int, int | None] = {target_id: None}
    stack = [target_id]
    found = False
    while stack and not found:
        node = stack.pop()
        for nxt in adjacency.get(node, []):
            if nxt in parent:
                continue
            parent[nxt] = node
            if nxt == source_id:
                found = True
                break
            stack.append(nxt)
    if not found:
        return None
    chain = [source_id]
    node = parent[source_id]
    while node is not None:
        chain.append(node)
        node = parent[node]
    # chain 是从 source 沿父指针回到 target 的逆序，反转后才是沿边方向 target→…→source
    path = [source_id] + list(reversed(chain))
    codes = load_claim_codes(db, set(path))
    return [codes[item] for item in path]


def propagate_impact(db: sqlite3.Connection, project_id: int, cause: dict[str, Any]) -> list[str]:
    """沿 证据→论点→论点 依赖图传播失效影响，返回本次新受影响的论点编码。

    被撤回的论点保持 retracted 终态，不向下游传播；受影响论点只记录原因，绝不删除。
    """
    stamp = now()
    affected: dict[int, list[dict[str, Any]]] = {}

    def mark(claim_id: int, entry: dict[str, Any]) -> None:
        entries = affected.setdefault(claim_id, [])
        if not any(item == entry for item in entries):
            entries.append(entry)

    if cause["type"] == "evidence":
        rows = db.execute(
            "SELECT DISTINCT e.source_claim_id FROM claim_edges e JOIN evidence_resources r ON r.id=e.evidence_version_id "
            "WHERE e.evidence_version_id IS NOT NULL AND r.project_id=? AND r.ref_code=?",
            (project_id, cause["ref_code"]),
        ).fetchall()
        entry = {"type": "evidence", "ref_code": cause["ref_code"], "event": cause["event"], "reason": cause["reason"]}
        for row in rows:
            mark(row["source_claim_id"], entry)
    else:
        entry = {"type": "claim", "claim_code": cause["claim_code"], "event": cause["event"], "reason": cause["reason"]}
        rows = db.execute("SELECT source_claim_id FROM claim_edges WHERE target_claim_id=?", (cause["claim_id"],)).fetchall()
        for row in rows:
            mark(row["source_claim_id"], entry)

    queue = list(affected)
    while queue:
        claim_id = queue.pop(0)
        row = db.execute("SELECT claim_code,status FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None or row["status"] == "retracted":
            continue
        downstream = db.execute("SELECT source_claim_id FROM claim_edges WHERE target_claim_id=?", (claim_id,)).fetchall()
        entry = {"type": "claim", "claim_code": row["claim_code"], "event": "affected", "reason": "上游论点证据失效"}
        for item in downstream:
            before = len(affected.get(item["source_claim_id"], []))
            mark(item["source_claim_id"], entry)
            if len(affected.get(item["source_claim_id"], [])) > before:
                queue.append(item["source_claim_id"])

    changed: list[str] = []
    for claim_id, entries in affected.items():
        row = db.execute("SELECT status,affected_reason FROM claims WHERE id=?", (claim_id,)).fetchone()
        if row is None or row["status"] == "retracted":
            continue
        existing = json.loads(row["affected_reason"]) if row["affected_reason"] else []
        merged = existing + [item for item in entries if item not in existing]
        if merged == existing:
            continue
        db.execute("UPDATE claims SET status='affected',affected_reason=?,affected_at=?,updated_at=? WHERE id=?", (stable_json(merged), stamp, stamp, claim_id))
        code = db.execute("SELECT claim_code FROM claims WHERE id=?", (claim_id,)).fetchone()["claim_code"]
        changed.append(code)
    return sorted(changed)
