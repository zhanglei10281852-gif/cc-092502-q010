from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40)
    name: str = Field(..., min_length=1, max_length=120)
    site_name: str = Field(..., min_length=1, max_length=120)


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    display_name: str = Field(..., min_length=1, max_length=80)
    password: str = Field(..., min_length=10, max_length=200)


class LoginRequest(BaseModel):
    username: str
    password: str


class MemberCreate(BaseModel):
    user_id: int
    role: str = Field(..., pattern="^(owner|researcher|recorder|reviewer|viewer)$")


class JobCreate(BaseModel):
    project_id: int | None = None
    job_type: str = Field(..., min_length=1, max_length=80)
    job_key: str = Field(..., min_length=1, max_length=160)
    input: dict = Field(default_factory=dict)


class JobFinish(BaseModel):
    worker_id: str = Field(..., min_length=1, max_length=80)
    result: dict = Field(default_factory=dict)


class ResourceCreate(BaseModel):
    kind: Literal["sample", "feature", "stat"]
    code: str = Field(..., min_length=1, max_length=60)
    title: str = Field(..., min_length=1, max_length=200)
    data: dict[str, Any] = Field(default_factory=dict)
    visibility: Literal["project", "restricted"] = "project"


class ResourceVersionCreate(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)


class ClaimCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=60)
    title: str = Field(..., min_length=1, max_length=200)
    statement: str = Field(..., min_length=1, max_length=4000)


class ClaimUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    statement: str | None = Field(default=None, min_length=1, max_length=4000)
    reason: str = Field(default="", max_length=500)


class EdgeCreate(BaseModel):
    edge_type: Literal["supports", "rebuts", "qualifies"]
    target_claim_id: int | None = None
    target_resource_id: int | None = None
    resource_version: int | None = Field(default=None, ge=1)


class EdgeRepin(BaseModel):
    version: int = Field(..., ge=1)


class ReviewPolicyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    quorum: int = Field(..., ge=1, le=100)
    reviewer_roles: list[Literal["owner", "researcher", "recorder", "reviewer", "viewer"]] = Field(default_factory=lambda: ["owner", "reviewer"], min_length=1)


class ArgumentCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = Field(default="", max_length=4000)
    claim_ids: list[int] = Field(default_factory=list)


class ArgumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)
    claim_ids: list[int] | None = None


class ReviewCreate(BaseModel):
    decision: Literal["approve", "request_changes"]
    comment: str = Field(default="", max_length=2000)
