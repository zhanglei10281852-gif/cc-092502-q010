"""部分证据不可见时的安全投影：受限资源对无权限角色只暴露占位信息。"""

from __future__ import annotations

from tests.test_evidence import make_claim, make_edge, make_resource


def setup_restricted(client, team):
    project_id = team["project"]["id"]
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    public = make_resource(client, project_id, team["members"]["recorder"]["headers"], code="S-PUB", visibility="project")
    secret = make_resource(client, project_id, owner, code="S-SEC", visibility="restricted", data={"gold": True, "position": "M3 墓底"})
    claim = make_claim(client, project_id, researcher, "C-VIS", "墓葬等级较高")
    make_edge(client, claim["id"], researcher, target_resource_id=public["id"])
    make_edge(client, claim["id"], researcher, edge_type="supports", target_resource_id=secret["id"])
    return {"project_id": project_id, "public": public, "secret": secret, "claim": claim}


def test_restricted_resource_projection(client, team):
    ctx = setup_restricted(client, team)
    secret_id = ctx["secret"]["id"]
    # owner / researcher 可见完整内容
    for role in ("owner", "researcher"):
        detail = client.get(f"/api/resources/{secret_id}", headers=team["members"][role]["headers"]).json()
        assert detail["redacted"] is False
        assert detail["versions"][0]["data"]["gold"] is True
    # recorder / reviewer / viewer 只能看到安全投影
    for role in ("recorder", "reviewer", "viewer"):
        detail = client.get(f"/api/resources/{secret_id}", headers=team["members"][role]["headers"]).json()
        assert detail["redacted"] is True
        assert detail["title"] == "[已屏蔽]"
        assert detail["versions"][0]["data"] is None
        assert detail["versions"][0]["content_hash"] is None
        assert "gold" not in str(detail)
    # 列表投影同样生效，且公开资源不受影响
    listing = client.get(f"/api/projects/{ctx['project_id']}/resources", headers=team["members"]["viewer"]["headers"]).json()["data"]
    by_code = {item["code"]: item for item in listing}
    assert by_code["S-SEC"]["redacted"] is True
    assert by_code["S-PUB"]["redacted"] is False


def test_claim_edges_projection(client, team):
    ctx = setup_restricted(client, team)
    detail = client.get(f"/api/claims/{ctx['claim']['id']}", headers=team["members"]["viewer"]["headers"]).json()
    targets = {edge["target"]["code"]: edge["target"] for edge in detail["edges"]}
    assert targets["S-SEC"]["redacted"] is True
    assert targets["S-SEC"]["title"] == "[已屏蔽]"
    assert targets["S-PUB"]["redacted"] is False
    # 钉版与失效信息仍然可见（论证链可追踪），只是内容被屏蔽
    assert targets["S-SEC"]["pinned_version"] == 1
    assert targets["S-SEC"]["stale"] is False


def test_restricted_diff_projection(client, team):
    ctx = setup_restricted(client, team)
    owner = team["members"]["owner"]["headers"]
    client.post(f"/api/resources/{ctx['secret']['id']}/versions", json={"data": {"gold": False}}, headers=owner)
    diff = client.get(f"/api/resources/{ctx['secret']['id']}/diff?from=1&to=2", headers=owner).json()
    assert diff["redacted"] is False and diff["changes"]
    projected = client.get(f"/api/resources/{ctx['secret']['id']}/diff?from=1&to=2", headers=team["members"]["viewer"]["headers"]).json()
    assert projected["redacted"] is True
    assert projected["changes"] is None
    assert projected["from_hash"] is None


def test_publication_projection(client, team):
    ctx = setup_restricted(client, team)
    project_id = ctx["project_id"]
    owner = team["members"]["owner"]["headers"]
    researcher = team["members"]["researcher"]["headers"]
    client.post(f"/api/projects/{project_id}/review-policies", json={"name": "单人评审", "quorum": 1, "reviewer_roles": ["owner"]}, headers=owner)
    argument = client.post(f"/api/projects/{project_id}/arguments", json={"title": "等级论证", "summary": "", "claim_ids": [ctx["claim"]["id"]]}, headers=researcher).json()
    client.post(f"/api/arguments/{argument['id']}/submit", headers=researcher)
    client.post(f"/api/arguments/{argument['id']}/reviews", json={"decision": "approve"}, headers=owner)
    published = client.post(f"/api/arguments/{argument['id']}/publish", headers=owner).json()
    # owner 视角：完整证据与版本哈希
    full = client.get(f"/api/arguments/{argument['id']}/publication", headers=owner).json()["package"]
    secret_evidence = [e for e in full["evidence"] if e["resource_code"] == "S-SEC"][0]
    assert secret_evidence["version_hash"]
    assert "projection" not in full
    assert full["content_hash"] == published["content_hash"]
    # viewer 视角：受限证据被屏蔽，清单哈希仍对应完整包
    projected = client.get(f"/api/arguments/{argument['id']}/publication", headers=team["members"]["viewer"]["headers"]).json()["package"]
    assert projected["projection"] == "redacted"
    masked = [e for e in projected["evidence"] if e["resource_code"] == "S-SEC"][0]
    assert masked["title"] == "[已屏蔽]"
    assert masked["version_hash"] is None
    assert masked["redacted"] is True
    public_evidence = [e for e in projected["evidence"] if e["resource_code"] == "S-PUB"][0]
    assert public_evidence["version_hash"]
    assert "gold" not in str(projected)
