"""受限证据的安全投影：无权限读者只能看到哈希占位，看不到摘要与内容。"""


def _restricted_setup(client, team):
    pid = team["project_id"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{pid}/evidence", json={"kind": "sample", "ref_code": "PUB-1", "summary": "公开样品", "content": {"w": 1}}, headers=researcher)
    client.post(f"/api/projects/{pid}/evidence", json={"kind": "sample", "ref_code": "SEC-1", "summary": "未公开的测年样本", "content": {"lab_no": "SECRET-LAB-77"}, "visibility": "restricted"}, headers=researcher)
    client.post(f"/api/projects/{pid}/claims", json={"claim_code": "C1", "title": "公开论点", "statement": "基于公开样品"}, headers=researcher)
    client.post(f"/api/projects/{pid}/claims", json={"claim_code": "C2", "title": "依赖受限证据的论点", "statement": "测年结论"}, headers=researcher)
    client.post(f"/api/projects/{pid}/edges", json={"source_claim_code": "C1", "relation": "supports", "evidence_ref": "PUB-1", "evidence_version": 1}, headers=researcher)
    client.post(f"/api/projects/{pid}/edges", json={"source_claim_code": "C2", "relation": "supports", "evidence_ref": "SEC-1", "evidence_version": 1}, headers=researcher)
    return pid


def test_restricted_evidence_redacted_for_viewer(client, team):
    pid = _restricted_setup(client, team)
    viewer = team["members"]["viewer"]["headers"]
    data = client.get(f"/api/projects/{pid}/evidence", headers=viewer).json()["data"]
    restricted = next(item for item in data if item["ref_code"] == "SEC-1")
    assert restricted["redacted"] is True
    assert "summary" not in restricted and "content" not in restricted
    assert restricted["content_hash"]  # 哈希占位仍然可见，便于校验
    public = next(item for item in data if item["ref_code"] == "PUB-1")
    assert public["summary"] == "公开样品"
    # 特权角色可见完整内容
    researcher = team["members"]["researcher"]["headers"]
    full = client.get(f"/api/projects/{pid}/evidence/SEC-1", headers=researcher).json()
    assert full["summary"] == "未公开的测年样本"


def test_restricted_edge_summary_redacted(client, team):
    pid = _restricted_setup(client, team)
    viewer = team["members"]["viewer"]["headers"]
    edges = client.get(f"/api/projects/{pid}/edges", headers=viewer).json()["data"]
    restricted_edge = next(item for item in edges if item.get("evidence_ref") == "SEC-1")
    assert restricted_edge["evidence_redacted"] is True
    assert "evidence_summary" not in restricted_edge
    assert restricted_edge["evidence_content_hash"]
    public_edge = next(item for item in edges if item.get("evidence_ref") == "PUB-1")
    assert public_edge["evidence_summary"] == "公开样品"


def test_affected_reasons_hide_restricted_evidence(client, team):
    pid = _restricted_setup(client, team)
    owner = team["members"]["owner"]["headers"]
    client.post(f"/api/projects/{pid}/evidence/SEC-1/withdraw", json={"reason": "实验室撤回报告"}, headers=owner)
    viewer = team["members"]["viewer"]["headers"]
    claim = client.get(f"/api/projects/{pid}/claims/C2", headers=viewer).json()
    assert claim["status"] == "affected"
    assert claim["affected_reasons"] == []  # 受限证据的失效原因不泄露给无权限读者
    researcher = team["members"]["researcher"]["headers"]
    full = client.get(f"/api/projects/{pid}/claims/C2", headers=researcher).json()
    assert full["affected_reasons"][0]["ref_code"] == "SEC-1"


def test_publication_projection_for_unprivileged(client, team):
    pid = _restricted_setup(client, team)
    researcher = team["members"]["researcher"]["headers"]
    reviewer = team["members"]["reviewer"]["headers"]
    manuscript = client.post(f"/api/projects/{pid}/manuscripts", json={"title": "论证稿", "claim_codes": ["C1", "C2"]}, headers=researcher).json()
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/submit", headers=researcher)
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/reviews", json={"decision": "approve", "comment": ""}, headers=reviewer)
    client.post(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/publish", headers=reviewer)
    # reviewer 不是特权角色：发布包中受限证据被移除并标注
    projected = client.get(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/publication", headers=reviewer).json()
    assert projected["projection"] == "redacted"
    refs = [item["ref_code"] for item in projected["package"]["sections"]["evidence"]]
    assert refs == ["PUB-1"]
    assert projected["package"]["projection"]["redacted_evidence"] == ["SEC-1@v1"]
    assert projected["manifest"]["redacted_evidence_count"] == 1
    assert all(item["ref_code"] != "SEC-1" for item in projected["manifest"]["evidence"])
    secret_edge = next(item for item in projected["package"]["sections"]["edges"] if item.get("evidence_ref") == "SEC-1")
    assert "evidence_summary" not in secret_edge
    # owner 可见完整包
    owner = team["members"]["owner"]["headers"]
    full = client.get(f"/api/projects/{pid}/manuscripts/{manuscript['id']}/publication", headers=owner).json()
    assert full["projection"] == "full"
    refs = [item["ref_code"] for item in full["package"]["sections"]["evidence"]]
    assert sorted(refs) == ["PUB-1", "SEC-1"]
    # 投影不泄露秘密内容
    assert "SECRET-LAB-77" not in str(projected)
    assert "SECRET-LAB-77" in str(full)
