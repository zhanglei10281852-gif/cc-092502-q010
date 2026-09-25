"""审计哈希链、命令行校验与服务重启恢复。"""

import json
import os
import subprocess
import sys
from pathlib import Path


def _publish(client, team):
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    client.post(f"/api/projects/{pid}/evidence", json={"kind": "feature", "ref_code": "F-1", "summary": "灰坑遗迹", "content": {"depth": 2.1}}, headers=researcher)
    client.post(f"/api/projects/{pid}/claims", json={"claim_code": "C1", "title": "灰坑为储藏设施", "statement": "H1 出土遗物表明其为储藏坑"}, headers=researcher)
    client.post(f"/api/projects/{pid}/edges", json={"source_claim_code": "C1", "relation": "supports", "evidence_ref": "F-1", "evidence_version": 1}, headers=researcher)
    manuscript = client.post(f"/api/projects/{pid}/manuscripts", json={"title": "阶段认识一", "claim_codes": ["C1"]}, headers=researcher).json()
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/submit", headers=researcher)
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/reviews", json={"decision": "approve", "comment": "同意"}, headers=reviewer)
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/publish", headers=reviewer)
    return pid, manuscript["id"]


def _run_cli(db_path: str, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, ARCHAEOLOGY_DATABASE_PATH=db_path)
    return subprocess.run([sys.executable, "-m", "app.cli", *args], capture_output=True, text=True, env=env, cwd=Path(__file__).resolve().parent.parent)


def test_audit_hash_chain_verifies(client, team):
    _publish(client, team)
    db_path = os.environ["ARCHAEOLOGY_DATABASE_PATH"]
    result = _run_cli(db_path, "verify-audit")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and report["events"] > 0 and report["head"]


def test_audit_chain_detects_tampering(client, team):
    _publish(client, team)
    from app.database import connection
    connection().execute("UPDATE audit_events SET payload_json='{}' WHERE id=2")
    connection().commit()
    db_path = os.environ["ARCHAEOLOGY_DATABASE_PATH"]
    result = _run_cli(db_path, "verify-audit")
    assert result.returncode == 1
    assert "哈希校验失败" in result.stdout


def test_restart_recovery(client, team):
    """关闭并重新打开数据库（模拟服务重启）后，发布包与审计链仍可校验。"""
    pid, manuscript_id = _publish(client, team)
    before = client.get(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publication", headers=team["members"]["owner"]["headers"]).json()
    from app.database import close_connection
    close_connection()  # 模拟进程退出；下次访问重新建立连接并执行 init_db
    from app.database import connection, init_db
    init_db()
    after = client.get(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publication", headers=team["members"]["owner"]["headers"]).json()
    assert after["package"]["content_hash"] == before["package"]["content_hash"]
    assert after["manifest_hash"] == before["manifest_hash"]
    db_path = os.environ["ARCHAEOLOGY_DATABASE_PATH"]
    result = _run_cli(db_path, "restart-check")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and report["audit"]["ok"] and report["publications"]["publications"] == 1


def test_cli_export_and_offline_verify(client, team, tmp_path):
    pid, manuscript_id = _publish(client, team)
    db_path = os.environ["ARCHAEOLOGY_DATABASE_PATH"]
    out_dir = tmp_path / "exported"
    exported = _run_cli(db_path, "export-publication", "--manuscript-id", str(manuscript_id), "--out", str(out_dir))
    assert exported.returncode == 0, exported.stderr
    verified = _run_cli(db_path, "verify-publication", "--package", str(out_dir / "package.json"), "--manifest", str(out_dir / "manifest.json"))
    assert verified.returncode == 0, verified.stdout
    # 篡改导出文件后离线校验失败
    package_file = out_dir / "package.json"
    tampered = json.loads(package_file.read_text(encoding="utf-8"))
    tampered["sections"]["claims"][0]["title"] = "篡改"
    package_file.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    failed = _run_cli(db_path, "verify-publication", "--package", str(package_file), "--manifest", str(out_dir / "manifest.json"))
    assert failed.returncode == 1


def test_cli_evidence_diff(client, team):
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/evidence", json={"kind": "sample", "ref_code": "S-9", "summary": "初测", "content": {"w": 1}}, headers=researcher)
    client.post(f"/api/projects/{pid}/evidence/S-9/versions", json={"summary": "复测", "content": {"w": 2}}, headers=researcher)
    db_path = os.environ["ARCHAEOLOGY_DATABASE_PATH"]
    result = _run_cli(db_path, "evidence-diff", "--project-id", str(pid), "--ref-code", "S-9", "--from-version", "1", "--to-version", "2")
    assert result.returncode == 0, result.stderr
    diff = json.loads(result.stdout)
    assert {item["field"] for item in diff["changed"]} >= {"summary", "content"}
