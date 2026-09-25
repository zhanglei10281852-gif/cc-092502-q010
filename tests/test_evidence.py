"""证据资源、论点、论据边：权限、钉版、环路拒绝、失效传播、版本比较。"""

from __future__ import annotations


def make_resource(client, project_id, headers, code="S-001", kind="sample", data=None, visibility="project"):
    response = client.post(
        f"/api/projects/{project_id}/resources",
        json={"kind": kind, "code": code, "title": f"样品 {code}", "data": data or {"layer": "H1", "weight": 12.5}, "visibility": visibility},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_claim(client, project_id, headers, code, statement="该层位属于同一期"):
    response = client.post(f"/api/projects/{project_id}/claims", json={"code": code, "title": f"论点 {code}", "statement": statement}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def make_edge(client, claim_id, headers, edge_type="supports", **target):
    response = client.post(f"/api/claims/{claim_id}/edges", json={"edge_type": edge_type, **target}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def test_resource_permissions(client, team):
    project_id = team["project"]["id"]
    # 记录员可以登记样品，观察员不能
    forbidden = client.post(
        f"/api/projects/{project_id}/resources",
        json={"kind": "sample", "code": "S-100", "title": "陶片", "data": {}},
        headers=team["members"]["viewer"]["headers"],
    )
    assert forbidden.status_code == 403
    resource = make_resource(client, project_id, team["members"]["recorder"]["headers"], code="S-100")
    assert resource["current_version"] == 1
    # 非项目成员完全不能读取
    outsider = client.post("/api/users", json={"username": "outsider", "display_name": "局外人", "password": "Outsider!234"}).json()
    login = client.post("/api/sessions", json={"username": "outsider", "password": "Outsider!234"}).json()
    denied = client.get(f"/api/resources/{resource['id']}", headers={"Authorization": f"Bearer {login['token']}"})
    assert denied.status_code == 403


def test_resource_idempotency(client, team):
    project_id = team["project"]["id"]
    headers = {**team["members"]["recorder"]["headers"], "Idempotency-Key": "res-1"}
    payload = {"kind": "feature", "code": "F-1", "title": "灰坑", "data": {"depth": 80}}
    first = client.post(f"/api/projects/{project_id}/resources", json=payload, headers=headers)
    second = client.post(f"/api/projects/{project_id}/resources", json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    conflict = client.post(f"/api/projects/{project_id}/resources", json={**payload, "title": "另一个"}, headers=headers)
    assert conflict.status_code == 409


def test_claim_cycle_rejected_with_path(client, team):
    project_id = team["project"]["id"]
    headers = team["members"]["researcher"]["headers"]
    a = make_claim(client, project_id, headers, "C-A")
    b = make_claim(client, project_id, headers, "C-B")
    c = make_claim(client, project_id, headers, "C-C")
    make_edge(client, a["id"], headers, target_claim_id=b["id"])  # A 依赖 B
    make_edge(client, b["id"], headers, target_claim_id=c["id"])  # B 依赖 C
    # C 依赖 A 将成环：C -> A -> B -> C
    response = client.post(f"/api/claims/{c['id']}/edges", json={"edge_type": "supports", "target_claim_id": a["id"]}, headers=headers)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "cycle_detected"
    assert error["details"]["path_codes"] == ["C-C", "C-A", "C-B", "C-C"]
    # 自环同样被拒绝
    self_loop = client.post(f"/api/claims/{a['id']}/edges", json={"edge_type": "rebuts", "target_claim_id": a["id"]}, headers=headers)
    assert self_loop.status_code == 409
    assert self_loop.json()["error"]["details"]["path"] == [a["id"], a["id"]]


def test_edge_pins_resource_version(client, team):
    project_id = team["project"]["id"]
    headers = team["members"]["researcher"]["headers"]
    resource = make_resource(client, project_id, team["members"]["recorder"]["headers"])
    claim = make_claim(client, project_id, headers, "C-PIN")
    edge = make_edge(client, claim["id"], headers, target_resource_id=resource["id"])
    assert edge["target"]["pinned_version"] == 1
    assert edge["target"]["stale"] is False
    # 显式钉住旧版本
    client.post(f"/api/resources/{resource['id']}/versions", json={"data": {"layer": "H2"}}, headers=team["members"]["recorder"]["headers"])
    edge2 = make_edge(client, claim["id"], headers, edge_type="qualifies", target_resource_id=resource["id"], resource_version=1)
    assert edge2["target"]["pinned_version"] == 1
    assert edge2["target"]["current_version"] == 2
    assert edge2["target"]["stale"] is True
    missing = client.post(f"/api/claims/{claim['id']}/edges", json={"edge_type": "supports", "target_resource_id": resource["id"], "resource_version": 99}, headers=headers)
    assert missing.status_code == 404


def test_invalidation_propagates_and_preserves_history(client, team):
    project_id = team["project"]["id"]
    researcher = team["members"]["researcher"]["headers"]
    resource = make_resource(client, project_id, team["members"]["recorder"]["headers"], code="S-9")
    x = make_claim(client, project_id, researcher, "C-X")
    y = make_claim(client, project_id, researcher, "C-Y")
    z = make_claim(client, project_id, researcher, "C-Z")
    make_edge(client, x["id"], researcher, target_resource_id=resource["id"])  # X 依据样品
    make_edge(client, y["id"], researcher, target_claim_id=x["id"])  # Y 依赖 X
    make_edge(client, z["id"], researcher, target_claim_id=y["id"])  # Z 依赖 Y
    # 替换资源版本：引用 v1 的论点受影响，并沿依赖图传播到 Y、Z
    client.post(f"/api/resources/{resource['id']}/versions", json={"data": {"layer": "H1", "weight": 13.1}}, headers=team["members"]["recorder"]["headers"])
    for claim_id, expect in [(x["id"], True), (y["id"], True), (z["id"], True)]:
        detail = client.get(f"/api/claims/{claim_id}", headers=researcher).json()
        assert detail["affected"] is expect, detail
        assert detail["affected_reason"]
    detail_x = client.get(f"/api/claims/{x['id']}", headers=researcher).json()
    assert "v1" in detail_x["affected_reason"] and "v2" in detail_x["affected_reason"]
    # 历史结论不被删除：论点与修订历史仍在
    assert detail_x["id"] == x["id"]
    assert detail_x["impacts"], "应记录失效影响事件"
    revisions = client.get(f"/api/claims/{x['id']}/revisions", headers=researcher).json()["data"]
    assert len(revisions) == 1
    affected_list = client.get(f"/api/projects/{project_id}/claims?affected=true", headers=researcher).json()["data"]
    assert {item["code"] for item in affected_list} == {"C-X", "C-Y", "C-Z"}


def test_withdraw_and_repin_resolve(client, team):
    project_id = team["project"]["id"]
    researcher = team["members"]["researcher"]["headers"]
    recorder = team["members"]["recorder"]["headers"]
    resource = make_resource(client, project_id, recorder, code="S-77")
    claim = make_claim(client, project_id, researcher, "C-W")
    edge = make_edge(client, claim["id"], researcher, target_resource_id=resource["id"])
    # 撤回资源 -> 论点受影响；撤回的资源不能再被引用
    client.post(f"/api/resources/{resource['id']}/withdraw", headers=recorder)
    detail = client.get(f"/api/claims/{claim['id']}", headers=researcher).json()
    assert detail["affected"] is True and "撤回" in detail["affected_reason"]
    blocked = client.post(f"/api/claims/{claim['id']}/edges", json={"edge_type": "supports", "target_resource_id": resource["id"]}, headers=researcher)
    assert blocked.status_code == 409
    repin_blocked = client.post(f"/api/edges/{edge['id']}/repin", json={"version": 1}, headers=researcher)
    assert repin_blocked.status_code == 409
    # 撤回该边后重算，论点恢复有效
    client.post(f"/api/edges/{edge['id']}/withdraw", headers=researcher)
    resolved = client.post(f"/api/claims/{claim['id']}/resolve", headers=researcher).json()
    assert resolved["affected"] is False
    causes = [item["cause"] for item in resolved["impacts"]]
    assert causes == ["invalidated", "resolved"]


def test_repin_to_new_version_resolves(client, team):
    project_id = team["project"]["id"]
    researcher = team["members"]["researcher"]["headers"]
    recorder = team["members"]["recorder"]["headers"]
    resource = make_resource(client, project_id, recorder, code="S-88")
    claim = make_claim(client, project_id, researcher, "C-R")
    edge = make_edge(client, claim["id"], researcher, target_resource_id=resource["id"])
    client.post(f"/api/resources/{resource['id']}/versions", json={"data": {"layer": "H3"}}, headers=recorder)
    assert client.get(f"/api/claims/{claim['id']}", headers=researcher).json()["affected"] is True
    repinned = client.post(f"/api/edges/{edge['id']}/repin", json={"version": 2}, headers=researcher)
    assert repinned.status_code == 200
    assert repinned.json()["target"]["pinned_version"] == 2
    assert repinned.json()["target"]["stale"] is False
    assert client.get(f"/api/claims/{claim['id']}", headers=researcher).json()["affected"] is False


def test_resource_version_diff(client, team):
    project_id = team["project"]["id"]
    recorder = team["members"]["recorder"]["headers"]
    resource = make_resource(client, project_id, recorder, code="ST-1", kind="stat", data={"count": 10, "sites": ["A", "B"], "meta": {"method": "freq"}})
    client.post(f"/api/resources/{resource['id']}/versions", json={"data": {"count": 12, "sites": ["A", "C"], "meta": {"method": "freq", "note": "复测"}}}, headers=recorder)
    diff = client.get(f"/api/resources/{resource['id']}/diff?from=1&to=2", headers=recorder).json()
    assert diff["redacted"] is False
    by_path = {change["path"]: change for change in diff["changes"]}
    assert by_path["/count"] == {"op": "change", "path": "/count", "from": 10, "to": 12}
    assert by_path["/sites/1"]["to"] == "C"
    assert by_path["/meta/note"]["op"] == "add"
    assert diff["from_hash"] != diff["to_hash"]


def test_claim_revision_history(client, team):
    project_id = team["project"]["id"]
    researcher = team["members"]["researcher"]["headers"]
    claim = make_claim(client, project_id, researcher, "C-HIST")
    updated = client.patch(f"/api/claims/{claim['id']}", json={"statement": "修正后的表述", "reason": "新测年数据"}, headers=researcher)
    assert updated.status_code == 200
    assert updated.json()["revision"] == 2
    revisions = client.get(f"/api/claims/{claim['id']}/revisions", headers=researcher).json()["data"]
    assert [row["revision"] for row in revisions] == [1, 2]
    assert revisions[1]["reason"] == "新测年数据"
    noop = client.patch(f"/api/claims/{claim['id']}", json={"statement": "修正后的表述"}, headers=researcher)
    assert noop.status_code == 409


def test_claim_write_permission(client, team):
    project_id = team["project"]["id"]
    for role in ("recorder", "reviewer", "viewer"):
        response = client.post(f"/api/projects/{project_id}/claims", json={"code": f"C-{role}", "title": "越权", "statement": "x"}, headers=team["members"][role]["headers"])
        assert response.status_code == 403, role
