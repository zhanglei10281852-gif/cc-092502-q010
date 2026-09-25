"""服务重启恢复、审计哈希链校验与命令行校验命令。"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from tests.test_evidence import make_claim, make_edge, make_resource


def restart(client):
    """模拟服务重启：关闭连接并以同一数据库文件重新进入应用。"""
    from app.database import close_connection
    close_connection()
    from app.main import app
    return TestClient(app)


def build_argument(client, team):
    project_id = team["project"]["id"]
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{project_id}/review-policies", json={"name": "单人评审", "quorum": 1, "reviewer_roles": ["owner"]}, headers=owner)
    resource = make_resource(client, project_id, team["members"]["recorder"]["headers"], code="S-RE")
    claim = make_claim(client, project_id, researcher, "C-RE", "重启前论点")
    make_edge(client, claim["id"], researcher, target_resource_id=resource["id"])
    argument = client.post(f"/api/projects/{project_id}/arguments", json={"title": "重启论证", "summary": "", "claim_ids": [claim["id"]]}, headers=researcher).json()
    client.post(f"/api/arguments/{argument['id']}/submit", headers=researcher)
    return argument


def test_restart_recovers_workflow(client, team):
    argument = build_argument(client, team)
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    with restart(client) as client2:
        # 会话、论点、送审状态在重启后仍然有效
        detail = client2.get(f"/api/arguments/{argument['id']}", headers=researcher)
        assert detail.status_code == 200
        assert detail.json()["status"] == "submitted"
        # 审批与发布可以继续
        approve = client2.post(f"/api/arguments/{argument['id']}/reviews", json={"decision": "approve"}, headers=owner)
        assert approve.status_code == 201
        published = client2.post(f"/api/arguments/{argument['id']}/publish", headers=owner)
        assert published.status_code == 200
        content_hash = published.json()["content_hash"]
    # 再次重启：发布包仍可读取且哈希一致
    with restart(client) as client3:
        publication = client3.get(f"/api/arguments/{argument['id']}/publication", headers=owner)
        assert publication.status_code == 200
        assert publication.json()["content_hash"] == content_hash


def test_restart_recovers_leased_jobs(client, team):
    project_id = team["project"]["id"]
    owner = team["members"]["owner"]["headers"]
    client.post("/api/jobs", json={"project_id": project_id, "job_type": "index", "job_key": "index:restart", "input": {}}, headers=owner)
    claimed = client.post("/api/jobs/claim?worker_id=w1").json()["job"]
    assert claimed["status"] == "leased"
    with restart(client):
        pass
    # 重启后中断的租约被回收，任务可被重新领取
    reclaimed = client.post("/api/jobs/claim?worker_id=w2").json()["job"]
    assert reclaimed["id"] == claimed["id"]
    done = client.post(f"/api/jobs/{claimed['id']}/finish", json={"worker_id": "w2", "result": {"ok": True}})
    assert done.json()["status"] == "done"


def test_audit_chain_detects_tampering(client, team):
    build_argument(client, team)
    verified = client.get("/api/audit/verify", headers=team["members"]["owner"]["headers"])
    assert verified.json()["valid"] is True
    assert verified.json()["checked"] > 0
    # 直接篡改数据库中的历史审计载荷，链条校验必须失败
    from app.database import connection
    first = connection().execute("SELECT id FROM audit_events ORDER BY id LIMIT 1").fetchone()
    connection().execute("UPDATE audit_events SET payload_json='{\"tampered\":true}' WHERE id=?", (first[0],))
    broken = client.get("/api/audit/verify", headers=team["members"]["owner"]["headers"]).json()
    assert broken["valid"] is False
    assert broken["first_bad_id"] == first[0]


def test_cli_verify_commands(client, team, tmp_path, capsys):
    argument = build_argument(client, team)
    owner = team["members"]["owner"]["headers"]
    client.post(f"/api/arguments/{argument['id']}/reviews", json={"decision": "approve"}, headers=owner)
    published = client.post(f"/api/arguments/{argument['id']}/publish", headers=owner).json()

    from app.cli import main as cli_main
    import sys

    def run_cli(*argv):
        old = sys.argv
        sys.argv = ["app.cli", *argv]
        try:
            return cli_main()
        finally:
            sys.argv = old

    assert run_cli("verify-audit") == 0
    audit_result = json.loads(capsys.readouterr().out)
    assert audit_result["valid"] is True

    publication_id = published["id"]
    assert run_cli("verify-publication", str(publication_id)) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["valid"] is True
    assert verified["stored_hash_matches"] is True

    out_file = tmp_path / "package.json"
    assert run_cli("export-publication", str(publication_id), "--out", str(out_file)) == 0
    capsys.readouterr()
    # 离线校验：只凭导出文件即可验证清单与内容哈希
    assert run_cli("verify-package", str(out_file)) == 0
    offline = json.loads(capsys.readouterr().out)
    assert offline["valid"] is True
    # 篡改导出文件后离线校验失败
    package = json.loads(out_file.read_text(encoding="utf-8"))
    package["claims"][0]["statement"] = "被篡改的结论"
    out_file.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
    assert run_cli("verify-package", str(out_file)) == 1
    tampered = json.loads(capsys.readouterr().out)
    assert tampered["valid"] is False
