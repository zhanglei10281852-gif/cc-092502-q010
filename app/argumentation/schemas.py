from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EvidenceKind = Literal["sample", "feature", "statistic", "document", "other"]
Relation = Literal["supports", "rebuts", "qualifies"]


class EvidenceCreate(BaseModel):
    kind: EvidenceKind
    ref_code: str = Field(..., min_length=1, max_length=80)
    summary: str = Field(..., min_length=1, max_length=2000)
    content: dict[str, Any] = Field(default_factory=dict)
    visibility: Literal["project", "restricted"] = "project"


class EvidenceVersionCreate(BaseModel):
    summary: str = Field(..., min_length=1, max_length=2000)
    content: dict[str, Any] = Field(default_factory=dict)
    replace: bool = False
    reason: str = Field(default="", max_length=500)


class EvidenceWithdraw(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)


class ClaimCreate(BaseModel):
    claim_code: str = Field(..., min_length=1, max_length=80)
    title: str = Field(..., min_length=1, max_length=200)
    statement: str = Field(..., min_length=1, max_length=4000)


class EdgeCreate(BaseModel):
    source_claim_code: str = Field(..., min_length=1, max_length=80)
    relation: Relation
    evidence_ref: str | None = Field(default=None, max_length=80)
    evidence_version: int | None = Field(default=None, ge=1)
    target_claim_code: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=1000)


class StrategyCreate(BaseModel):
    quorum: int = Field(..., ge=1, le=100)
    eligible_roles: list[str] = Field(..., min_length=1)


class ManuscriptCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    claim_codes: list[str] = Field(..., min_length=1, max_length=500)


class ManuscriptUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    claim_codes: list[str] | None = Field(default=None, min_length=1, max_length=500)


class ReviewCreate(BaseModel):
    decision: Literal["approve", "request_changes"]
    comment: str = Field(default="", max_length=2000)


class ManuscriptWithdraw(BaseModel):
    reason: str = Field(..., min_length=1, max_length=500)
