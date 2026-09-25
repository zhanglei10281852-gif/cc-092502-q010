"""发布包：稳定排序、内容哈希、可离线校验的清单，以及受限证据的安全投影。

发布包是确定性纯函数的产物：同样的数据库内容必然得到同样的字节。
清单（manifest）记录每个证据条目的内容哈希与整体包哈希，校验端不需要
访问服务即可确认包未被篡改；对无权限查看受限证据的读者，投影函数只保留
哈希占位，不泄露摘要与内容。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.security import stable_json

PACKAGE_FORMAT = "archaeology-argument-package/1"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _section_hash(value: Any) -> str:
    return _sha256(stable_json(value))


def build_package(db: sqlite3.Connection, manuscript: sqlite3.Row) -> dict[str, Any]:
    """根据稿件快照中的论点集合构建确定性发布包（含受限证据完整内容）。"""
    project_id = manuscript["project_id"]
    claim_ids = json.loads(manuscript["claim_ids_json"])
    claims: list[dict[str, Any]] = []
    if claim_ids:
        marks = ",".join("?" for _ in claim_ids)
        rows = db.execute(f"SELECT * FROM claims WHERE id IN ({marks}) ORDER BY claim_code", tuple(claim_ids)).fetchall()
        claims = [
            {
                "claim_code": row["claim_code"],
                "title": row["title"],
                "statement": row["statement"],
                "status": row["status"],
                "affected_reasons": json.loads(row["affected_reason"]) if row["affected_reason"] else [],
            }
            for row in rows
        ]
    edges: list[dict[str, Any]] = []
    dissents: list[dict[str, Any]] = []
    if claim_ids:
        marks = ",".join("?" for _ in claim_ids)
        rows = db.execute(
            f"SELECT * FROM claim_edges WHERE source_claim_id IN ({marks}) ORDER BY id",
            tuple(claim_ids),
        ).fetchall()
        code_of = {row["id"]: row["claim_code"] for row in db.execute("SELECT id,claim_code FROM claims").fetchall()}
        for row in rows:
            entry: dict[str, Any] = {
                "source_claim_code": code_of.get(row["source_claim_id"]),
                "relation": row["relation"],
                "note": row["note"],
            }
            if row["target_type"] == "evidence":
                evidence = db.execute("SELECT * FROM evidence_resources WHERE id=?", (row["evidence_version_id"],)).fetchone()
                entry.update({
                    "target_type": "evidence",
                    "evidence_ref": evidence["ref_code"],
                    "evidence_version": evidence["version"],
                    "evidence_status": evidence["status"],
                    "evidence_visibility": evidence["visibility"],
                    "evidence_summary": evidence["summary"],
                    "evidence_content_hash": evidence["content_hash"],
                })
            else:
                entry.update({"target_type": "claim", "target_claim_code": code_of.get(row["target_claim_id"])})
            edges.append(entry)
            if row["relation"] in ("rebuts", "qualifies"):
                dissents.append(dict(entry))
    edges.sort(key=lambda item: (item["source_claim_code"] or "", item["relation"], item.get("evidence_ref") or item.get("target_claim_code") or ""))
    dissents.sort(key=lambda item: (item["source_claim_code"] or "", item["relation"], item.get("evidence_ref") or item.get("target_claim_code") or ""))

    evidence_entries: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for edge in edges:
        if edge["target_type"] != "evidence":
            continue
        key = (edge["evidence_ref"], edge["evidence_version"])
        if key in seen:
            continue
        seen.add(key)
        row = db.execute(
            "SELECT * FROM evidence_resources WHERE project_id=? AND ref_code=? AND version=?",
            (project_id, edge["evidence_ref"], edge["evidence_version"]),
        ).fetchone()
        evidence_entries.append({
            "ref_code": row["ref_code"],
            "version": row["version"],
            "kind": row["kind"],
            "status": row["status"],
            "visibility": row["visibility"],
            "summary": row["summary"],
            "content": json.loads(row["content_json"]),
            "content_hash": row["content_hash"],
        })
    evidence_entries.sort(key=lambda item: (item["ref_code"], item["version"]))

    cycle = None
    if manuscript["review_cycle_id"]:
        cycle = db.execute("SELECT * FROM review_cycles WHERE id=?", (manuscript["review_cycle_id"],)).fetchone()
    reviews: list[dict[str, Any]] = []
    if cycle:
        rows = db.execute(
            "SELECT r.decision,r.comment,r.created_at,u.username FROM manuscript_reviews r JOIN users u ON u.id=r.reviewer_id WHERE r.cycle_id=? ORDER BY u.username",
            (cycle["id"],),
        ).fetchall()
        reviews = [{"reviewer": row["username"], "decision": row["decision"], "comment": row["comment"], "created_at": row["created_at"]} for row in rows]

    sections = {
        "claims": claims,
        "edges": edges,
        "evidence": evidence_entries,
        "dissents": dissents,
        "reviews": reviews,
    }
    section_hashes = {name: _section_hash(value) for name, value in sections.items()}
    strategy = None
    if cycle:
        strategy = {"quorum": cycle["quorum_snapshot"], "eligible_roles": json.loads(cycle["eligible_roles_json"])}
    package = {
        "format": PACKAGE_FORMAT,
        "manuscript": {"id": manuscript["id"], "title": manuscript["title"], "version": manuscript["version"]},
        "project_id": project_id,
        "review_strategy": strategy,
        "sections": sections,
        "section_hashes": section_hashes,
    }
    package["content_hash"] = _section_hash({"sections": sections, "manuscript": package["manuscript"], "review_strategy": strategy})
    return package


def build_manifest(package: dict[str, Any]) -> dict[str, Any]:
    entries = [
        {"ref_code": item["ref_code"], "version": item["version"], "content_hash": item["content_hash"], "status": item["status"], "visibility": item["visibility"]}
        for item in package["sections"]["evidence"]
    ]
    return {
        "format": PACKAGE_FORMAT,
        "manuscript_id": package["manuscript"]["id"],
        "package_content_hash": package["content_hash"],
        "section_hashes": package["section_hashes"],
        "evidence": entries,
        "counts": {name: len(package["sections"][name]) for name in ("claims", "edges", "evidence", "dissents", "reviews")},
    }


def manifest_hash(manifest: dict[str, Any]) -> str:
    return _sha256(stable_json(manifest))


def project_package(package: dict[str, Any], privileged: bool) -> dict[str, Any]:
    """面向无权限读者的安全投影：受限证据只保留哈希占位，并整体重算哈希。"""
    if privileged:
        return package
    projected = json.loads(stable_json(package))
    evidence = projected["sections"]["evidence"]
    redacted: list[str] = []
    kept: list[dict[str, Any]] = []
    for item in evidence:
        if item["visibility"] == "restricted":
            redacted.append(f"{item['ref_code']}@v{item['version']}")
        else:
            kept.append(item)
    projected["sections"]["evidence"] = kept
    for edge in projected["sections"]["edges"]:
        if edge.get("target_type") == "evidence" and edge.get("evidence_visibility") == "restricted":
            edge.pop("evidence_summary", None)
            edge["evidence_redacted"] = True
    for dissent in projected["sections"]["dissents"]:
        if dissent.get("target_type") == "evidence" and dissent.get("evidence_visibility") == "restricted":
            dissent.pop("evidence_summary", None)
            dissent["evidence_redacted"] = True
    for claim in projected["sections"]["claims"]:
        claim["affected_reasons"] = [item for item in claim["affected_reasons"] if item.get("type") != "evidence" or not _is_restricted_ref(package, item.get("ref_code", ""))]
    projected["section_hashes"] = {name: _section_hash(value) for name, value in projected["sections"].items()}
    projected["content_hash"] = _section_hash({"sections": projected["sections"], "manuscript": projected["manuscript"], "review_strategy": projected["review_strategy"]})
    projected["projection"] = {"privileged": False, "redacted_evidence": sorted(redacted)}
    return projected


def _is_restricted_ref(package: dict[str, Any], ref_code: str) -> bool:
    for item in package["sections"]["evidence"]:
        if item["ref_code"] == ref_code:
            return item["visibility"] == "restricted"
    return True


def project_manifest(manifest: dict[str, Any], package: dict[str, Any], privileged: bool) -> dict[str, Any]:
    if privileged:
        return manifest
    projected = json.loads(stable_json(manifest))
    restricted = [item for item in projected["evidence"] if item["visibility"] == "restricted"]
    projected["evidence"] = [item for item in projected["evidence"] if item["visibility"] != "restricted"]
    projected["section_hashes"] = package["section_hashes"]
    projected["package_content_hash"] = package["content_hash"]
    projected["counts"]["evidence"] = len(projected["evidence"])
    projected["redacted_evidence_count"] = len(restricted)
    return projected


def verify_package(package: dict[str, Any], manifest: dict[str, Any]) -> list[str]:
    """离线校验发布包与清单，返回问题列表（空列表表示通过）。"""
    problems: list[str] = []
    if package.get("format") != PACKAGE_FORMAT:
        problems.append(f"未知的包格式: {package.get('format')}")
    sections = package.get("sections", {})
    for name in ("claims", "edges", "evidence", "dissents", "reviews"):
        expected = package.get("section_hashes", {}).get(name)
        actual = _section_hash(sections.get(name))
        if expected != actual:
            problems.append(f"段落哈希不匹配: {name}")
    recomputed = _section_hash({"sections": sections, "manuscript": package.get("manuscript"), "review_strategy": package.get("review_strategy")})
    if recomputed != package.get("content_hash"):
        problems.append("包内容哈希不匹配")
    if manifest.get("package_content_hash") != package.get("content_hash"):
        problems.append("清单与发布包内容哈希不一致")
    for name in ("claims", "edges", "evidence", "dissents", "reviews"):
        if manifest.get("section_hashes", {}).get(name) != package.get("section_hashes", {}).get(name):
            problems.append(f"清单段落哈希不一致: {name}")
    by_key = {(item["ref_code"], item["version"]): item for item in sections.get("evidence", [])}
    for entry in manifest.get("evidence", []):
        item = by_key.get((entry["ref_code"], entry["version"]))
        if item is None:
            problems.append(f"清单中的证据在包内缺失: {entry['ref_code']}@v{entry['version']}")
            continue
        if item["content_hash"] != entry["content_hash"]:
            problems.append(f"证据内容哈希不匹配: {entry['ref_code']}@v{entry['version']}")
        if item["content_hash"] != _section_hash({"summary": item["summary"], "content": item["content"]}):
            problems.append(f"证据内容哈希无法复算: {entry['ref_code']}@v{entry['version']}")
    if len(by_key) != len(manifest.get("evidence", [])):
        problems.append("包内证据数量与清单不一致")
    return problems
