from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app.database import connection, init_db, recover_interrupted_jobs


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_init_db() -> int:
    init_db()
    _print({"status": "initialized"})
    return 0


def cmd_check_db() -> int:
    init_db()
    db = connection()
    _print({"integrity": db.execute("PRAGMA integrity_check").fetchone()[0], "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "tables": db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]})
    return 0


def cmd_smoke() -> int:
    from app.main import app
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
        _print({"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]})
    return 0


def cmd_create_user(args: argparse.Namespace) -> int:
    init_db()
    from app.service import ResearchService
    user = ResearchService().create_user({"username": args.username, "display_name": args.display_name, "password": args.password})
    _print(user)
    return 0


def cmd_recover_jobs() -> int:
    init_db()
    recovered = recover_interrupted_jobs(connection())
    _print({"recovered": recovered})
    return 0


def cmd_verify_audit(args: argparse.Namespace) -> int:
    init_db()
    from app.service import BaseService
    result = BaseService().verify_audit_chain(args.project_id)
    _print(result)
    return 0 if result["valid"] else 1


def _load_publication(publication_id: int) -> dict:
    init_db()
    row = connection().execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
    if row is None:
        raise SystemExit(f"发布记录 {publication_id} 不存在")
    return dict(row)


def cmd_export_publication(args: argparse.Namespace) -> int:
    row = _load_publication(args.publication_id)
    package = row["package_json"]
    if args.out:
        Path(args.out).write_text(package, encoding="utf-8")
        _print({"exported": args.out, "publication_id": row["id"], "content_hash": row["content_hash"]})
    else:
        print(package)
    return 0


def cmd_verify_publication(args: argparse.Namespace) -> int:
    from app.publication import verify_package
    row = _load_publication(args.publication_id)
    package = json.loads(row["package_json"])
    result = verify_package(package)
    result["publication_id"] = row["id"]
    result["stored_content_hash"] = row["content_hash"]
    result["stored_hash_matches"] = row["content_hash"] == package.get("content_hash")
    result["valid"] = result["valid"] and result["stored_hash_matches"]
    _print(result)
    return 0 if result["valid"] else 1


def cmd_verify_package(args: argparse.Namespace) -> int:
    """离线校验：只读取导出的发布包文件，不访问数据库。"""
    from app.publication import verify_package
    package = json.loads(Path(args.file).read_text(encoding="utf-8"))
    result = verify_package(package)
    _print(result)
    return 0 if result["valid"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="考古研究论证与发布服务命令行")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("check-db")
    sub.add_parser("smoke")
    sub.add_parser("recover-jobs")

    create_user = sub.add_parser("create-user")
    create_user.add_argument("--username", required=True)
    create_user.add_argument("--display-name", required=True)
    create_user.add_argument("--password", required=True)

    verify_audit = sub.add_parser("verify-audit")
    verify_audit.add_argument("--project-id", type=int, default=None)

    export = sub.add_parser("export-publication")
    export.add_argument("publication_id", type=int)
    export.add_argument("--out", default="")

    verify_publication = sub.add_parser("verify-publication")
    verify_publication.add_argument("publication_id", type=int)

    verify_package = sub.add_parser("verify-package")
    verify_package.add_argument("file")

    args = parser.parse_args()
    handlers = {
        "init-db": lambda: cmd_init_db(),
        "check-db": lambda: cmd_check_db(),
        "smoke": lambda: cmd_smoke(),
        "recover-jobs": lambda: cmd_recover_jobs(),
        "create-user": lambda: cmd_create_user(args),
        "verify-audit": lambda: cmd_verify_audit(args),
        "export-publication": lambda: cmd_export_publication(args),
        "verify-publication": lambda: cmd_verify_publication(args),
        "verify-package": lambda: cmd_verify_package(args),
    }
    return handlers[args.command]()


if __name__ == "__main__":
    sys.exit(main())
