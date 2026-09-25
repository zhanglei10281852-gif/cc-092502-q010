from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PASSWORD = "TeamPass!2345"


@pytest.fixture()
def client(tmp_path: Path):
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = str(tmp_path / "test.db")
    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as value:
        yield value
    close_connection()


def register(client, username: str, display_name: str) -> dict:
    user = client.post("/api/users", json={"username": username, "display_name": display_name, "password": PASSWORD})
    assert user.status_code == 201, user.text
    login = client.post("/api/sessions", json={"username": username, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"user": user.json(), "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def owner(client):
    return register(client, "owner", "项目负责人")


@pytest.fixture()
def team(client, owner):
    """一个项目 + 五种角色成员，返回 {project, members: {role: {user, headers}}}。"""
    project = client.post("/api/projects", json={"code": "TEAM", "name": "团队项目", "site_name": "遗址"}, headers=owner["headers"]).json()
    members = {"owner": owner}
    for role, display in [("researcher", "研究员"), ("recorder", "记录员"), ("reviewer", "评审员"), ("viewer", "观察员")]:
        member = register(client, f"u_{role}", display)
        response = client.post(f"/api/projects/{project['id']}/members", json={"user_id": member["user"]["id"], "role": role}, headers=owner["headers"])
        assert response.status_code == 200, response.text
        members[role] = member
    return {"project": project, "members": members}
