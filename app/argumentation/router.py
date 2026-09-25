"""研究论证与发布模块的 HTTP 路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.argumentation.claims import ClaimService
from app.argumentation.evidence import EvidenceService
from app.argumentation.manuscripts import ManuscriptService
from app.argumentation.schemas import (
    ClaimCreate,
    EdgeCreate,
    EvidenceCreate,
    EvidenceVersionCreate,
    EvidenceWithdraw,
    ManuscriptCreate,
    ManuscriptUpdate,
    ManuscriptWithdraw,
    ReviewCreate,
    StrategyCreate,
)
from app.service import ResearchService

router = APIRouter(prefix="/api/projects/{project_id}", tags=["argumentation"])


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])


# -- 证据 ------------------------------------------------------------------
@router.post("/evidence", status_code=201)
def create_evidence(project_id: int, payload: EvidenceCreate, user=Depends(current_user)):
    return EvidenceService().create_evidence(project_id, user["id"], payload.model_dump())


@router.get("/evidence")
def list_evidence(project_id: int, ref_code: str | None = Query(default=None), user=Depends(current_user)):
    return {"data": EvidenceService().list_evidence(project_id, user["id"], ref_code)}


@router.get("/evidence/diff")
def diff_evidence(project_id: int, ref_code: str = Query(...), from_version: int = Query(...), to_version: int = Query(...), user=Depends(current_user)):
    return EvidenceService().diff_versions(project_id, user["id"], ref_code, from_version, to_version)


@router.get("/evidence/{ref_code}")
def get_evidence(project_id: int, ref_code: str, version: int | None = Query(default=None), user=Depends(current_user)):
    return EvidenceService().get_evidence(project_id, user["id"], ref_code, version)


@router.post("/evidence/{ref_code}/versions", status_code=201)
def add_evidence_version(project_id: int, ref_code: str, payload: EvidenceVersionCreate, user=Depends(current_user)):
    return EvidenceService().add_version(project_id, user["id"], ref_code, payload.model_dump())


@router.post("/evidence/{ref_code}/withdraw")
def withdraw_evidence(project_id: int, ref_code: str, payload: EvidenceWithdraw, user=Depends(current_user)):
    return EvidenceService().withdraw(project_id, user["id"], ref_code, payload.reason)


# -- 论点与边 ----------------------------------------------------------------
@router.post("/claims", status_code=201)
def create_claim(project_id: int, payload: ClaimCreate, user=Depends(current_user)):
    return ClaimService().create_claim(project_id, user["id"], payload.model_dump())


@router.get("/claims")
def list_claims(project_id: int, user=Depends(current_user)):
    return {"data": ClaimService().list_claims(project_id, user["id"])}


@router.get("/claims/affected")
def affected_claims(project_id: int, user=Depends(current_user)):
    return {"data": ClaimService().affected_claims(project_id, user["id"])}


@router.get("/claims/{claim_code}")
def get_claim(project_id: int, claim_code: str, user=Depends(current_user)):
    return ClaimService().get_claim(project_id, user["id"], claim_code)


@router.post("/claims/{claim_code}/retract")
def retract_claim(project_id: int, claim_code: str, payload: ManuscriptWithdraw, user=Depends(current_user)):
    return ClaimService().retract_claim(project_id, user["id"], claim_code, payload.reason)


@router.post("/edges", status_code=201)
def add_edge(project_id: int, payload: EdgeCreate, user=Depends(current_user)):
    return ClaimService().add_edge(project_id, user["id"], payload.model_dump())


@router.get("/edges")
def list_edges(project_id: int, claim_code: str | None = Query(default=None), user=Depends(current_user)):
    return {"data": ClaimService().list_edges(project_id, user["id"], claim_code)}


# -- 评审策略 ----------------------------------------------------------------
@router.put("/review-strategy")
def set_strategy(project_id: int, payload: StrategyCreate, user=Depends(current_user)):
    return ManuscriptService().set_strategy(project_id, user["id"], payload.model_dump())


@router.get("/review-strategy")
def get_strategy(project_id: int, user=Depends(current_user)):
    return ManuscriptService().get_strategy(project_id, user["id"])


# -- 稿件 ------------------------------------------------------------------
@router.post("/manuscripts", status_code=201)
def create_manuscript(project_id: int, payload: ManuscriptCreate, user=Depends(current_user)):
    return ManuscriptService().create_manuscript(project_id, user["id"], payload.model_dump())


@router.get("/manuscripts")
def list_manuscripts(project_id: int, user=Depends(current_user)):
    return {"data": ManuscriptService().list_manuscripts(project_id, user["id"])}


@router.get("/manuscripts/{manuscript_id}")
def get_manuscript(project_id: int, manuscript_id: int, user=Depends(current_user)):
    return ManuscriptService().get_manuscript(project_id, user["id"], manuscript_id)


@router.patch("/manuscripts/{manuscript_id}")
def update_manuscript(project_id: int, manuscript_id: int, payload: ManuscriptUpdate, user=Depends(current_user)):
    return ManuscriptService().update_manuscript(project_id, user["id"], manuscript_id, payload.model_dump(exclude_unset=True))


@router.post("/manuscripts/{manuscript_id}/submit")
def submit_manuscript(project_id: int, manuscript_id: int, user=Depends(current_user)):
    return ManuscriptService().submit(project_id, user["id"], manuscript_id)


@router.post("/manuscripts/{manuscript_id}/reviews", status_code=201)
def review_manuscript(project_id: int, manuscript_id: int, payload: ReviewCreate, user=Depends(current_user)):
    return ManuscriptService().review(project_id, user["id"], manuscript_id, payload.decision, payload.comment)


@router.post("/manuscripts/{manuscript_id}/publish")
def publish_manuscript(project_id: int, manuscript_id: int, user=Depends(current_user)):
    return ManuscriptService().publish(project_id, user["id"], manuscript_id)


@router.post("/manuscripts/{manuscript_id}/withdraw")
def withdraw_manuscript(project_id: int, manuscript_id: int, payload: ManuscriptWithdraw, user=Depends(current_user)):
    return ManuscriptService().withdraw(project_id, user["id"], manuscript_id, payload.reason)


@router.get("/manuscripts/{manuscript_id}/publication")
def get_publication(project_id: int, manuscript_id: int, user=Depends(current_user)):
    return ManuscriptService().get_publication(project_id, user["id"], manuscript_id)
