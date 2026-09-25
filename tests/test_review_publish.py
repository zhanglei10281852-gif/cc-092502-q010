"""论证稿状态机、评审法定人数快照、幂等发布、发布包内容与异议。"""

from __future__ import annotations

import threading

from tests.test_evidence import make_claim, make_edge, make_resource


def setup_argument(client, team, quorum=2):
    """建策略、两个论点、一条证据边、一份论证稿并送审，返回上下文。"""
    project_id = team["project"]["id"]
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    recorder = team["members"]["recorder"]["headers"]
    policy = client.post(f"/api/projects/{project_id}/review-policies", json={"name": "双评审", "quorum": quorum, "reviewer_roles": ["owner", "reviewer"]}, headers=owner)
    assert policy.status_code == 201, policy.text
    resource = make_resource(client, project_id, recorder, code="S-PUB")
    c1 = make_claim(client, project_id, researcher, "C-01", "第一期遗存早于第二期")
    c2 = make_claim(client, project_id, researcher, "C-02", "H1 灰坑为储藏坑")
    make_edge(client, c1["id"], researcher, target_resource_id=resource["id"])
    argument = client.post(
        f"/api/projects/{project_id}/arguments",
        json={"title": "第一期遗存性质论证", "summary": "基于层位与样品", "claim_ids": [c2["id"], c1["id"]]},
        headers=researcher,
    )
    assert argument.status_code == 201, argument.text
    return {"project_id": project_id, "argument": argument.json(), "claims": [c1, c2], "resource": resource}


def test_full_workflow_and_package(client, team):
    ctx = setup_argument(client, team)
    argument_id = ctx["argument"]["id"]
    owner = team["members"]["owner"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    # 草拟状态不能评审
    early = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=reviewer)
    assert early.status_code == 409
    submitted = client.post(f"/api/arguments/{argument_id}/submit", headers=researcher).json()
    assert submitted["status"] == "submitted"
    assert submitted["policy_snapshot"]["quorum"] == 2
    # 第一次批准未达法定人数
    first = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve", "comment": "同意"}, headers=owner)
    assert first.status_code == 201
    assert first.json()["argument_status"] == "submitted"
    second = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=reviewer)
    assert second.json()["argument_status"] == "approved"
    # 发布后状态与包内容
    published = client.post(f"/api/arguments/{argument_id}/publish", headers=owner)
    assert published.status_code == 200, published.text
    body = published.json()
    package = body["package"]
    assert body["replayed"] is False
    assert [claim["code"] for claim in package["claims"]] == ["C-01", "C-02"]  # 稳定排序
    assert package["evidence"][0]["pinned_version"] == 1
    assert package["evidence"][0]["version_hash"]
    assert package["content_hash"] == body["content_hash"]
    assert package["manifest"]["content_hash"] == package["content_hash"]
    assert set(package["manifest"]["sections"]) == {"argument", "policy_snapshot", "approvals", "claims", "evidence", "objections", "warnings"}
    assert len(package["approvals"]) == 2
    detail = client.get(f"/api/arguments/{argument_id}", headers=owner).json()
    assert detail["status"] == "published"
    assert detail["publication"]["content_hash"] == package["content_hash"]


def test_author_cannot_approve_own_argument(client, team):
    ctx = setup_argument(client, team)
    argument_id = ctx["argument"]["id"]
    researcher = team["members"]["researcher"]["headers"]
    owner = team["members"]["owner"]["headers"]
    # 把作者所在角色加入评审策略后送审：作者仍不能批准自己的稿件
    client.post(f"/api/projects/{ctx['project_id']}/review-policies", json={"name": "全员评审", "quorum": 2, "reviewer_roles": ["owner", "researcher", "reviewer"]}, headers=owner)
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    response = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=researcher)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "self_review"


def test_quorum_uses_submission_snapshot(client, team):
    ctx = setup_argument(client, team, quorum=2)
    argument_id = ctx["argument"]["id"]
    owner = team["members"]["owner"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    # 送审后把策略法定人数改为 1：本次评审仍按快照 quorum=2 计算
    client.post(f"/api/projects/{ctx['project_id']}/review-policies", json={"name": "单人评审", "quorum": 1, "reviewer_roles": ["owner", "reviewer"]}, headers=owner)
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=owner)
    detail = client.get(f"/api/arguments/{argument_id}", headers=owner).json()
    assert detail["status"] == "submitted"
    assert detail["approvals"] == {"count": 1, "quorum": 2}
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=reviewer)
    assert client.get(f"/api/arguments/{argument_id}", headers=owner).json()["status"] == "approved"


def test_request_changes_then_resubmit(client, team):
    ctx = setup_argument(client, team)
    argument_id = ctx["argument"]["id"]
    owner = team["members"]["owner"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    changes = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "request_changes", "comment": "补充测年数据"}, headers=reviewer)
    assert changes.json()["argument_status"] == "changes_requested"
    # 修改后产生新修订版，比较两个修订版
    new_claim = make_claim(client, ctx["project_id"], researcher, "C-03", "补充的测年论点")
    updated = client.put(f"/api/arguments/{argument_id}", json={"claim_ids": [c["id"] for c in ctx["claims"]] + [new_claim["id"]], "summary": "已补充测年"}, headers=researcher)
    assert updated.json()["current_revision"] == 2
    diff = client.get(f"/api/arguments/{argument_id}/diff?from=1&to=2", headers=researcher).json()
    assert diff["claims_added"] == [new_claim["id"]]
    assert diff["summary_changed"] is True
    resubmitted = client.post(f"/api/arguments/{argument_id}/submit", headers=researcher).json()
    assert resubmitted["status"] == "submitted"
    assert resubmitted["approvals"]["count"] == 0  # 旧修订版的批准不计入
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=owner)
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=reviewer)
    assert client.get(f"/api/arguments/{argument_id}", headers=owner).json()["status"] == "approved"


def test_duplicate_review_and_publish_are_idempotent(client, team):
    ctx = setup_argument(client, team, quorum=1)
    argument_id = ctx["argument"]["id"]
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    first = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=owner)
    replay = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=owner)
    assert first.json()["id"] == replay.json()["id"]
    assert replay.json()["replayed"] is True
    conflict = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "request_changes"}, headers=owner)
    assert conflict.status_code == 409
    detail = client.get(f"/api/arguments/{argument_id}", headers=owner).json()
    assert len(detail["reviews"]) == 1
    assert detail["status"] == "approved"
    # 重复发布回调返回同一发布记录
    publish1 = client.post(f"/api/arguments/{argument_id}/publish", headers=owner)
    publish2 = client.post(f"/api/arguments/{argument_id}/publish", headers=owner)
    assert publish1.status_code == publish2.status_code == 200
    assert publish1.json()["id"] == publish2.json()["id"]
    assert publish2.json()["replayed"] is True
    from app.database import connection
    count = connection().execute("SELECT COUNT(*) FROM publications WHERE argument_id=?", (argument_id,)).fetchone()[0]
    assert count == 1


def test_concurrent_approvals_and_publish_never_duplicate(client, team):
    ctx = setup_argument(client, team, quorum=2)
    argument_id = ctx["argument"]["id"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    owner_id = team["members"]["owner"]["user"]["id"]
    reviewer_id = team["members"]["reviewer"]["user"]["id"]

    from app.publication import PublicationService

    barrier = threading.Barrier(2)
    review_results: list[dict] = []

    def do_review(user_id):
        barrier.wait()
        review_results.append(PublicationService().review(argument_id, {"decision": "approve", "comment": ""}, user_id))

    threads = [threading.Thread(target=do_review, args=(uid,)) for uid in (owner_id, reviewer_id)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(review_results) == 2
    assert client.get(f"/api/arguments/{argument_id}", headers=researcher).json()["status"] == "approved"

    publish_results: list[dict] = []
    barrier2 = threading.Barrier(2)

    def do_publish():
        barrier2.wait()
        publish_results.append(PublicationService().publish(argument_id, owner_id))

    threads = [threading.Thread(target=do_publish) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(publish_results) == 2
    assert {result["id"] for result in publish_results} == {publish_results[0]["id"]}
    assert sum(1 for result in publish_results if result["replayed"]) == 1
    from app.database import connection
    assert connection().execute("SELECT COUNT(*) FROM publications WHERE argument_id=?", (argument_id,)).fetchone()[0] == 1
    approved_events = connection().execute("SELECT COUNT(*) FROM audit_events WHERE action='argument.approved' AND resource_id=?", (str(argument_id),)).fetchone()[0]
    assert approved_events == 1


def test_retract_keeps_publication_history(client, team):
    ctx = setup_argument(client, team, quorum=1)
    argument_id = ctx["argument"]["id"]
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=owner)
    client.post(f"/api/arguments/{argument_id}/publish", headers=owner)
    retracted = client.post(f"/api/arguments/{argument_id}/retract", headers=owner)
    assert retracted.json()["status"] == "retracted"
    # 发布包不删除，仍可追溯
    publication = client.get(f"/api/arguments/{argument_id}/publication", headers=owner)
    assert publication.status_code == 200
    again = client.post(f"/api/arguments/{argument_id}/publish", headers=owner)
    assert again.status_code == 409


def test_review_role_and_state_guards(client, team):
    ctx = setup_argument(client, team, quorum=1)
    argument_id = ctx["argument"]["id"]
    researcher = team["members"]["researcher"]["headers"]
    viewer = team["members"]["viewer"]["headers"]
    owner = team["members"]["owner"]["headers"]
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    # viewer 不在评审角色内
    denied = client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=viewer)
    assert denied.status_code == 403
    # 未批准不能发布；非 owner 不能发布
    not_ready = client.post(f"/api/arguments/{argument_id}/publish", headers=owner)
    assert not_ready.status_code == 409
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=owner)
    not_publisher = client.post(f"/api/arguments/{argument_id}/publish", headers=researcher)
    assert not_publisher.status_code == 403


def test_package_records_objections(client, team):
    ctx = setup_argument(client, team, quorum=1)
    project_id = ctx["project_id"]
    argument_id = ctx["argument"]["id"]
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    # 一条针对包内论点的反驳边（异议）
    counter = make_claim(client, project_id, researcher, "C-99", "反例：测年数据存在污染")
    make_edge(client, counter["id"], researcher, edge_type="rebuts", target_claim_id=ctx["claims"][0]["id"])
    # 先经历一轮要求修改，再批准发布：异议应进入发布包
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "request_changes", "comment": "需要讨论反例"}, headers=reviewer)
    client.post(f"/api/arguments/{argument_id}/submit", headers=researcher)
    client.post(f"/api/arguments/{argument_id}/reviews", json={"decision": "approve"}, headers=owner)
    published = client.post(f"/api/arguments/{argument_id}/publish", headers=owner).json()
    objections = published["package"]["objections"]
    assert objections["rebut_edges"][0]["from_claim_code"] == "C-99"
    assert objections["rebut_edges"][0]["target_claim_code"] == "C-01"
    assert objections["review_objections"][0]["comment"] == "需要讨论反例"
