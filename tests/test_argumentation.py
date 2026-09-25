"""论点、证据与依赖图：权限、版本固定、失效传播、环路拒绝。"""


def _evidence(client, team, ref="EV-1", role="researcher", **overrides):
    payload = {"kind": "sample", "ref_code": ref, "summary": f"证据{ref}", "content": {"weight": 10}}
    payload.update(overrides)
    response = client.post(f"/api/projects/{team['project_id']}/evidence", json=payload, headers=team["members"][role]["headers"])
    assert response.status_code == 201, response.json()
    return response.json()


def _claim(client, team, code, role="researcher", statement="论点陈述"):
    response = client.post(
        f"/api/projects/{team['project_id']}/claims",
        json={"claim_code": code, "title": f"论点{code}", "statement": statement},
        headers=team["members"][role]["headers"],
    )
    assert response.status_code == 201, response.json()
    return response.json()


def _edge(client, team, payload, role="researcher"):
    return client.post(f"/api/projects/{team['project_id']}/edges", json=payload, headers=team["members"][role]["headers"])


# -- 权限 ------------------------------------------------------------------
def test_viewer_cannot_write(client, team):
    pid = team["project_id"]
    viewer = team["members"]["viewer"]["headers"]
    assert client.post(f"/api/projects/{pid}/evidence", json={"kind": "sample", "ref_code": "X", "summary": "s"}, headers=viewer).status_code == 403
    assert client.post(f"/api/projects/{pid}/claims", json={"claim_code": "C1", "title": "t", "statement": "s"}, headers=viewer).status_code == 403
    assert client.get(f"/api/projects/{pid}/claims", headers=viewer).status_code == 200


def test_recorder_cannot_withdraw_evidence(client, team):
    pid = team["project_id"]
    _evidence(client, team)
    recorder = team["members"]["recorder"]["headers"]
    assert client.post(f"/api/projects/{pid}/evidence/EV-1/withdraw", json={"reason": "样本污染"}, headers=recorder).status_code == 403
    owner = team["members"]["owner"]["headers"]
    assert client.post(f"/api/projects/{pid}/evidence/EV-1/withdraw", json={"reason": "样本污染"}, headers=owner).status_code == 200


def test_non_member_forbidden(client, team):
    outsider = client.post("/api/users", json={"username": "outsider", "display_name": "局外人", "password": "Outsider!2345"}).json()
    login = client.post("/api/sessions", json={"username": "outsider", "password": "Outsider!2345"}).json()
    headers = {"Authorization": f"Bearer {login['token']}"}
    assert client.get(f"/api/projects/{team['project_id']}/claims", headers=headers).status_code == 403


# -- 证据版本与引用固定 --------------------------------------------------------
def test_edge_pins_evidence_version(client, team):
    pid = team["project_id"]
    _evidence(client, team)
    _claim(client, team, "C1")
    edge = _edge(client, team, {"source_claim_code": "C1", "relation": "supports", "evidence_ref": "EV-1", "evidence_version": 1})
    assert edge.status_code == 201, edge.json()
    assert edge.json()["evidence_version"] == 1
    assert edge.json()["evidence_content_hash"]
    # 追加新版本后，旧边仍固定引用 v1
    client.post(f"/api/projects/{pid}/evidence/EV-1/versions", json={"summary": "修订", "content": {"weight": 11}}, headers=team["members"]["researcher"]["headers"])
    edges = client.get(f"/api/projects/{pid}/edges?claim_code=C1", headers=team["members"]["viewer"]["headers"]).json()["data"]
    assert edges[0]["evidence_version"] == 1


def test_evidence_diff(client, team):
    pid = team["project_id"]
    _evidence(client, team)
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/evidence/EV-1/versions", json={"summary": "修订后", "content": {"weight": 12}}, headers=researcher)
    diff = client.get(f"/api/projects/{pid}/evidence/diff?ref_code=EV-1&from_version=1&to_version=2", headers=researcher).json()
    fields = {item["field"] for item in diff["changed"]}
    assert {"summary", "content", "content_hash"} <= fields


# -- 失效传播 ----------------------------------------------------------------
def test_withdraw_propagates_along_graph(client, team):
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    _evidence(client, team)
    _claim(client, team, "C1")
    _claim(client, team, "C2")
    _claim(client, team, "C3")
    _edge(client, team, {"source_claim_code": "C1", "relation": "supports", "evidence_ref": "EV-1", "evidence_version": 1})
    _edge(client, team, {"source_claim_code": "C2", "relation": "supports", "target_claim_code": "C1"})
    _edge(client, team, {"source_claim_code": "C3", "relation": "qualifies", "target_claim_code": "C2"})
    result = client.post(f"/api/projects/{pid}/evidence/EV-1/withdraw", json={"reason": "样品编号登记错误"}, headers=researcher).json()
    assert result["affected_claims"] == ["C1", "C2", "C3"]
    claims = {item["claim_code"]: item for item in client.get(f"/api/projects/{pid}/claims", headers=researcher).json()["data"]}
    assert all(claims[code]["status"] == "affected" for code in ("C1", "C2", "C3"))
    # 历史结论保留：论点与边仍然存在，未被删除
    assert claims["C1"]["statement"]
    assert len(client.get(f"/api/projects/{pid}/edges", headers=researcher).json()["data"]) == 3
    affected = client.get(f"/api/projects/{pid}/claims/affected", headers=researcher).json()["data"]
    assert {item["claim_code"] for item in affected} == {"C1", "C2", "C3"}
    reasons = affected[0]["affected_reasons"]
    assert any(item.get("ref_code") == "EV-1" and item.get("event") == "withdrawn" for item in reasons)


def test_replace_marks_old_version_superseded_and_propagates(client, team):
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    _evidence(client, team)
    _claim(client, team, "C1")
    _edge(client, team, {"source_claim_code": "C1", "relation": "supports", "evidence_ref": "EV-1", "evidence_version": 1})
    result = client.post(f"/api/projects/{pid}/evidence/EV-1/versions", json={"summary": "重测结果", "content": {"weight": 9}, "replace": True, "reason": "仪器校准错误"}, headers=researcher)
    assert result.status_code == 201
    assert result.json()["affected_claims"] == ["C1"]
    versions = client.get(f"/api/projects/{pid}/evidence?ref_code=EV-1", headers=researcher).json()["data"]
    assert versions[0]["status"] == "superseded" and versions[0]["superseded_by_id"] == versions[1]["id"]
    assert versions[1]["status"] == "active"
    claim = client.get(f"/api/projects/{pid}/claims/C1", headers=researcher).json()
    assert claim["status"] == "affected"
    assert claim["affected_reasons"][0]["event"] == "replaced"


def test_retracted_claim_not_reaffected_and_blocks_edges(client, team):
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    _evidence(client, team)
    _claim(client, team, "C1")
    _claim(client, team, "C2")
    _edge(client, team, {"source_claim_code": "C1", "relation": "supports", "evidence_ref": "EV-1", "evidence_version": 1})
    client.post(f"/api/projects/{pid}/claims/C1/retract", json={"reason": "作者主动撤回"}, headers=researcher)
    # 撤回是终态：之后的证据失效不再把它改回 affected
    client.post(f"/api/projects/{pid}/evidence/EV-1/withdraw", json={"reason": "撤回证据"}, headers=researcher)
    claim = client.get(f"/api/projects/{pid}/claims/C1", headers=researcher).json()
    assert claim["status"] == "retracted"
    # 不能依赖已撤回论点
    assert _edge(client, team, {"source_claim_code": "C2", "relation": "supports", "target_claim_code": "C1"}).status_code == 409


def test_duplicate_edge_rejected(client, team):
    _evidence(client, team)
    _claim(client, team, "C1")
    payload = {"source_claim_code": "C1", "relation": "supports", "evidence_ref": "EV-1", "evidence_version": 1}
    assert _edge(client, team, payload).status_code == 201
    assert _edge(client, team, payload).status_code == 409


# -- 环路拒绝 ----------------------------------------------------------------
def test_cycle_rejected_with_path(client, team):
    _claim(client, team, "A")
    _claim(client, team, "B")
    _claim(client, team, "C")
    assert _edge(client, team, {"source_claim_code": "A", "relation": "supports", "target_claim_code": "B"}).status_code == 201
    assert _edge(client, team, {"source_claim_code": "B", "relation": "supports", "target_claim_code": "C"}).status_code == 201
    response = _edge(client, team, {"source_claim_code": "C", "relation": "supports", "target_claim_code": "A"})
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "dependency_cycle"
    assert body["details"]["cycle_path"] == ["C", "A", "B", "C"]


def test_self_loop_rejected(client, team):
    _claim(client, team, "A")
    response = _edge(client, team, {"source_claim_code": "A", "relation": "supports", "target_claim_code": "A"})
    assert response.status_code == 409
    assert response.json()["error"]["details"]["cycle_path"] == ["A", "A"]
