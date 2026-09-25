import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    os.environ["ARCHAEOLOGY_DATABASE_PATH"] = str(tmp_path / "test.db")
    from app.database import close_connection
    close_connection()
    from app.main import app
    with TestClient(app) as value:
        yield value
    close_connection()


def _make_user(client, username: str, display: str) -> dict:
    password = f"Pass-{username}!234"
    user = client.post("/api/users", json={"username": username, "display_name": display, "password": password})
    assert user.status_code == 201
    login = client.post("/api/sessions", json={"username": username, "password": password})
    assert login.status_code == 200
    return {"user": user.json(), "headers": {"Authorization": f"Bearer {login.json()['token']}"}}


@pytest.fixture()
def owner(client):
    return _make_user(client, "owner", "项目负责人")


@pytest.fixture()
def team(client, owner):
    """一个项目 + 全角色成员，返回 {project_id, headers_by_role}。"""
    project = client.post("/api/projects", json={"code": "TEAM1", "name": "论证测试项目", "site_name": "测试遗址"}, headers=owner["headers"]).json()
    members = {"owner": owner}
    for role in ("researcher", "recorder", "reviewer", "viewer"):
        member = _make_user(client, f"u_{role}", f"角色{role}")
        client.post(f"/api/projects/{project['id']}/members", json={"user_id": member["user"]["id"], "role": role}, headers=owner["headers"])
        members[role] = member
    return {"project_id": project["id"], "members": members}
