"""评审流转：状态机、法定人数快照、自审禁令、幂等与并发发布。"""

import json
import threading

import pytest


def _setup_argument(client, team):
    """两个论点 + 一条证据边，返回论点编码列表。"""
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/evidence", json={"kind": "statistic", "ref_code": "ST-1", "summary": "陶片统计", "content": {"n": 42}}, headers=researcher)
    for code in ("C1", "C2"):
        client.post(f"/api/projects/{pid}/claims", json={"claim_code": code, "title": f"论点{code}", "statement": "阶段性认识"}, headers=researcher)
    client.post(f"/api/projects/{pid}/edges", json={"source_claim_code": "C1", "relation": "supports", "evidence_ref": "ST-1", "evidence_version": 1}, headers=researcher)
    client.post(f"/api/projects/{pid}/edges", json={"source_claim_code": "C2", "relation": "rebuts", "target_claim_code": "C1", "note": "测年数据冲突"}, headers=researcher)
    return ["C1", "C2"]


def _manuscript(client, team, claims=("C1", "C2"), author="researcher"):
    pid = team["project_id"]
    response = client.post(
        f"/api/projects/{pid}/manuscripts",
        json={"title": "第一阶段论证稿", "claim_codes": list(claims)},
        headers=team["members"][author]["headers"],
    )
    assert response.status_code == 201, response.json()
    return response.json()["id"]


def _review(client, team, manuscript_id, decision="approve", role="reviewer", comment=""):
    return client.post(
        f"/api/projects/{team['project_id']}/manuscripts/{manuscript_id}/reviews",
        json={"decision": decision, "comment": comment},
        headers=team["members"][role]["headers"],
    )


def test_full_lifecycle(client, team):
    pid = team["project_id"]
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    researcher = team["members"]["researcher"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    # 草稿不能直接发布
    assert client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publish", headers=reviewer).status_code == 409
    submitted = client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher).json()
    assert submitted["status"] == "in_review"
    assert submitted["review_cycle"]["quorum"] == 1
    approved = _review(client, team, manuscript_id, "approve", comment="同意发布")
    assert approved.status_code == 201 and approved.json()["status"] == "approved"
    published = client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publish", headers=reviewer).json()
    assert published["status"] == "published" and not published["duplicate"]
    publication = client.get(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publication", headers=reviewer).json()
    assert publication["package"]["content_hash"]
    withdrawn = client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/withdraw", json={"reason": "新证据出现"}, headers=researcher).json()
    assert withdrawn["status"] == "withdrawn"
    # 撤回后发布包仍可审计
    again = client.get(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publication", headers=reviewer).json()
    assert again["manuscript_status"] == "withdrawn" and again["package"]["content_hash"] == publication["package"]["content_hash"]


def test_author_cannot_approve_own_manuscript(client, team):
    pid = team["project_id"]
    owner = team["members"]["owner"]["headers"]
    client.put(f"/api/projects/{pid}/review-strategy", json={"quorum": 1, "eligible_roles": ["researcher", "reviewer"]}, headers=owner)
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)
    response = _review(client, team, manuscript_id, "approve", role="researcher")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "self_approval"


def test_quorum_uses_snapshot_not_current_strategy(client, team):
    pid = team["project_id"]
    owner = team["members"]["owner"]["headers"]
    # 第二名评审人
    second = client.post("/api/users", json={"username": "reviewer2", "display_name": "评审二", "password": "Reviewer2!234"}).json()
    client.post(f"/api/projects/{pid}/members", json={"user_id": second["id"], "role": "reviewer"}, headers=owner)
    login = client.post("/api/sessions", json={"username": "reviewer2", "password": "Reviewer2!234"}).json()
    reviewer2_headers = {"Authorization": f"Bearer {login['token']}"}
    client.put(f"/api/projects/{pid}/review-strategy", json={"quorum": 2, "eligible_roles": ["reviewer"]}, headers=owner)
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)
    # 送审后修改策略为 quorum=1，不影响进行中的评审
    client.put(f"/api/projects/{pid}/review-strategy", json={"quorum": 1, "eligible_roles": ["reviewer"]}, headers=owner)
    first = _review(client, team, manuscript_id, "approve").json()
    assert first["status"] == "in_review" and first["approvals"] == 1
    second_review = client.post(
        f"/api/projects/{pid}/manuscripts/{manuscript_id}/reviews",
        json={"decision": "approve", "comment": "同意"},
        headers=reviewer2_headers,
    ).json()
    assert second_review["status"] == "approved" and second_review["approvals"] == 2


def test_duplicate_review_callback_is_idempotent(client, team):
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)
    first = _review(client, team, manuscript_id, "approve")
    assert first.status_code == 201 and not first.json()["duplicate"]
    second = _review(client, team, manuscript_id, "approve")
    assert second.status_code == 201 and second.json()["duplicate"]
    detail = client.get(f"/api/projects/{pid}/manuscripts/{manuscript_id}", headers=researcher).json()
    assert len(detail["reviews"]) == 1


def test_request_changes_then_resubmit(client, team):
    pid = team["project_id"]
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)
    rejected = _review(client, team, manuscript_id, "request_changes", comment="补充层位信息").json()
    assert rejected["status"] == "changes_requested"
    # 同一评审人的重复回调幂等返回既有投票
    assert _review(client, team, manuscript_id, "approve").json()["duplicate"] is True
    # 已关闭的评审轮次拒绝其他评审人的新投票
    second = client.post("/api/users", json={"username": "reviewer3", "display_name": "评审三", "password": "Reviewer3!234"}).json()
    client.post(f"/api/projects/{pid}/members", json={"user_id": second["id"], "role": "reviewer"}, headers=team["members"]["owner"]["headers"])
    login = client.post("/api/sessions", json={"username": "reviewer3", "password": "Reviewer3!234"}).json()
    late = client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/reviews", json={"decision": "approve", "comment": ""}, headers={"Authorization": f"Bearer {login['token']}"})
    assert late.status_code == 409
    edited = client.patch(f"/api/projects/{pid}/manuscripts/{manuscript_id}", json={"title": "第一阶段论证稿（修订）"}, headers=researcher).json()
    assert edited["title"].endswith("（修订）")
    resubmitted = client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher).json()
    assert resubmitted["status"] == "in_review" and resubmitted["version"] == 3
    approved = _review(client, team, manuscript_id, "approve").json()
    assert approved["status"] == "approved"


def test_ineligible_role_cannot_review(client, team):
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)
    response = _review(client, team, manuscript_id, "approve", role="recorder")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_eligible"


def test_double_publish_returns_same_manifest(client, team):
    pid = team["project_id"]
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    researcher = team["members"]["researcher"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)
    _review(client, team, manuscript_id, "approve")
    first = client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publish", headers=reviewer).json()
    second = client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publish", headers=reviewer).json()
    assert not first["duplicate"] and second["duplicate"]
    assert first["manifest_hash"] == second["manifest_hash"]
    from app.database import connection
    count = connection().execute("SELECT COUNT(*) FROM publications WHERE manuscript_id=?", (manuscript_id,)).fetchone()[0]
    assert count == 1


def test_concurrent_approvals_and_publish_once(client, team):
    """服务层并发：两个评审人同时批准 + 两个发布请求同时到达，只发布一次。"""
    pid = team["project_id"]
    owner = team["members"]["owner"]["headers"]
    second = client.post("/api/users", json={"username": "reviewer2", "display_name": "评审二", "password": "Reviewer2!234"}).json()
    client.post(f"/api/projects/{pid}/members", json={"user_id": second["id"], "role": "reviewer"}, headers=owner)
    client.put(f"/api/projects/{pid}/review-strategy", json={"quorum": 2, "eligible_roles": ["reviewer"]}, headers=owner)
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)

    from app.argumentation.manuscripts import ManuscriptService
    from app.database import close_connection, connection

    reviewer1_id = team["members"]["reviewer"]["user"]["id"]
    reviewer2_id = second["id"]

    def run(fn):
        close_connection()  # 每个线程使用自己的连接
        try:
            return fn(ManuscriptService())
        finally:
            close_connection()

    results = {}
    errors = []

    def worker(name, fn):
        try:
            results[name] = run(fn)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("r1", lambda s: s.review(pid, reviewer1_id, manuscript_id, "approve", ""))),
        threading.Thread(target=worker, args=("r2", lambda s: s.review(pid, reviewer2_id, manuscript_id, "approve", ""))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    close_connection()
    status = ManuscriptService().get_manuscript(pid, reviewer1_id, manuscript_id)["status"]
    assert status == "approved"

    publish_results = []
    threads = [
        threading.Thread(target=lambda: publish_results.append(run(lambda s: s.publish(pid, reviewer1_id, manuscript_id)))),
        threading.Thread(target=lambda: publish_results.append(run(lambda s: s.publish(pid, reviewer2_id, manuscript_id)))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    close_connection()
    assert len(publish_results) == 2
    assert sum(1 for item in publish_results if not item["duplicate"]) == 1
    assert len({item["manifest_hash"] for item in publish_results}) == 1
    count = connection().execute("SELECT COUNT(*) FROM publications WHERE manuscript_id=?", (manuscript_id,)).fetchone()[0]
    assert count == 1


def test_package_stable_order_and_offline_verify(client, team, tmp_path):
    pid = team["project_id"]
    _setup_argument(client, team)
    manuscript_id = _manuscript(client, team)
    researcher = team["members"]["researcher"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/submit", headers=researcher)
    _review(client, team, manuscript_id, "approve")
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publish", headers=reviewer)
    publication = client.get(f"/api/projects/{pid}/manuscripts/{manuscript_id}/publication", headers=reviewer).json()
    package = publication["package"]
    codes = [item["claim_code"] for item in package["sections"]["claims"]]
    assert codes == sorted(codes)
    assert package["sections"]["dissents"][0]["relation"] == "rebuts"
    assert package["sections"]["reviews"][0]["decision"] == "approve"
    from app.argumentation.packaging import verify_package
    assert verify_package(package, publication["manifest"]) == []
    # 篡改任一字段即校验失败
    tampered = json.loads(json.dumps(package))
    tampered["sections"]["claims"][0]["statement"] = "被篡改的结论"
    problems = verify_package(tampered, publication["manifest"])
    assert problems
