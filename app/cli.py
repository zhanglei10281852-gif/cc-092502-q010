from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def _configure(args: argparse.Namespace) -> None:
    if getattr(args, "db", None):
        os.environ["ARCHAEOLOGY_DATABASE_PATH"] = args.db


def _verify_audit_chain() -> dict:
    from app.database import connection, init_db
    from app.security import audit_event_hash

    init_db()
    db = connection()
    rows = db.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
    prev = ""
    for row in rows:
        fields = {
            "project_id": row["project_id"],
            "actor_id": row["actor_id"],
            "action": row["action"],
            "resource_type": row["resource_type"],
            "resource_id": row["resource_id"],
            "payload_json": row["payload_json"],
            "prev_hash": row["prev_hash"],
            "created_at": row["created_at"],
        }
        if row["prev_hash"] != prev:
            return {"ok": False, "events": len(rows), "error": f"事件 {row['id']} 的前置哈希断链"}
        if audit_event_hash(prev, fields) != row["event_hash"]:
            return {"ok": False, "events": len(rows), "error": f"事件 {row['id']} 的哈希校验失败"}
        prev = row["event_hash"]
    return {"ok": True, "events": len(rows), "head": prev}


def _verify_publications() -> dict:
    import json as _json

    from app.argumentation.packaging import verify_package
    from app.database import connection, init_db

    init_db()
    db = connection()
    rows = db.execute("SELECT * FROM publications ORDER BY id").fetchall()
    problems: list[str] = []
    for row in rows:
        issues = verify_package(_json.loads(row["package_json"]), _json.loads(row["manifest_json"]))
        problems.extend(f"publication#{row['id']}: {issue}" for issue in issues)
    return {"ok": not problems, "publications": len(rows), "problems": problems}


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli")
    parser.add_argument("--db", help="数据库文件路径（等价于 ARCHAEOLOGY_DATABASE_PATH）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("check-db")
    sub.add_parser("smoke")
    sub.add_parser("verify-audit")
    sub.add_parser("restart-check")
    verify_pub = sub.add_parser("verify-publication")
    verify_pub.add_argument("--manuscript-id", type=int, help="从数据库校验指定稿件的发布包")
    verify_pub.add_argument("--package", help="离线校验导出的 package.json")
    verify_pub.add_argument("--manifest", help="离线校验导出的 manifest.json")
    export_pub = sub.add_parser("export-publication")
    export_pub.add_argument("--manuscript-id", type=int, required=True)
    export_pub.add_argument("--out", required=True, help="输出目录")
    diff = sub.add_parser("evidence-diff")
    diff.add_argument("--project-id", type=int, required=True)
    diff.add_argument("--ref-code", required=True)
    diff.add_argument("--from-version", type=int, required=True)
    diff.add_argument("--to-version", type=int, required=True)
    args = parser.parse_args()
    _configure(args)

    from app.database import connection, init_db

    if args.command == "init-db":
        init_db()
        print(json.dumps({"status": "initialized"}, ensure_ascii=False))
        return 0
    if args.command == "check-db":
        init_db()
        db = connection()
        print(json.dumps({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]}, ensure_ascii=False))
        return 0
    if args.command == "verify-audit":
        result = _verify_audit_chain()
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 1
    if args.command == "restart-check":
        # 服务重启恢复：重新打开数据库后，审计链与全部发布包必须仍然可校验
        audit = _verify_audit_chain()
        publications = _verify_publications()
        ok = audit["ok"] and publications["ok"]
        print(json.dumps({"ok": ok, "audit": audit, "publications": publications}, ensure_ascii=False))
        return 0 if ok else 1
    if args.command == "verify-publication":
        from app.argumentation.packaging import verify_package

        if args.package and args.manifest:
            package = json.loads(Path(args.package).read_text(encoding="utf-8"))
            manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        elif args.manuscript_id:
            init_db()
            row = connection().execute("SELECT * FROM publications WHERE manuscript_id=?", (args.manuscript_id,)).fetchone()
            if row is None:
                print(json.dumps({"ok": False, "error": "该稿件没有发布记录"}, ensure_ascii=False))
                return 1
            package, manifest = json.loads(row["package_json"]), json.loads(row["manifest_json"])
        else:
            print(json.dumps({"ok": False, "error": "需要 --manuscript-id 或 --package/--manifest"}, ensure_ascii=False))
            return 2
        problems = verify_package(package, manifest)
        print(json.dumps({"ok": not problems, "problems": problems, "content_hash": package.get("content_hash")}, ensure_ascii=False))
        return 0 if not problems else 1
    if args.command == "export-publication":
        init_db()
        row = connection().execute("SELECT * FROM publications WHERE manuscript_id=?", (args.manuscript_id,)).fetchone()
        if row is None:
            print(json.dumps({"ok": False, "error": "该稿件没有发布记录"}, ensure_ascii=False))
            return 1
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "package.json").write_text(row["package_json"], encoding="utf-8")
        (out / "manifest.json").write_text(row["manifest_json"], encoding="utf-8")
        print(json.dumps({"ok": True, "out": str(out), "manifest_hash": row["manifest_hash"]}, ensure_ascii=False))
        return 0
    if args.command == "evidence-diff":
        from app.argumentation.evidence import EvidenceService

        init_db()
        service = EvidenceService()
        project = connection().execute("SELECT id FROM projects WHERE id=?", (args.project_id,)).fetchone()
        if project is None:
            print(json.dumps({"ok": False, "error": "项目不存在"}, ensure_ascii=False))
            return 1
        diff_result = service.diff_versions(args.project_id, _cli_actor(args.project_id), args.ref_code, args.from_version, args.to_version)
        print(json.dumps(diff_result, ensure_ascii=False))
        return 0
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        print(json.dumps({"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}, ensure_ascii=False))
    return 0


def _cli_actor(project_id: int) -> int:
    """命令行以项目负责人的身份做只读版本比较（取任一 owner 成员）。"""
    from app.database import connection

    row = connection().execute("SELECT user_id FROM project_members WHERE project_id=? AND role='owner' ORDER BY joined_at LIMIT 1", (project_id,)).fetchone()
    if row is None:
        from app.service import ServiceError

        raise ServiceError("forbidden", "项目没有负责人，无法执行命令行比较", 403)
    return row["user_id"]


if __name__ == "__main__":
    sys.exit(main())
