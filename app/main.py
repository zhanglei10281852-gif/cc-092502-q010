from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse

from app.database import close_connection, connection, init_db
from app.evidence import EvidenceService
from app.publication import PublicationService
from app.schemas import (
    ArgumentCreate,
    ArgumentUpdate,
    ClaimCreate,
    ClaimUpdate,
    EdgeCreate,
    EdgeRepin,
    JobCreate,
    JobFinish,
    LoginRequest,
    MemberCreate,
    ProjectCreate,
    ResourceCreate,
    ResourceVersionCreate,
    ReviewCreate,
    ReviewPolicyCreate,
    UserCreate,
)
from app.service import BaseService, ResearchService, ServiceError


@asynccontextmanager
async def lifespan(app: FastAPI):
    del app
    init_db()
    yield
    close_connection()


app = FastAPI(title="考古研究论证与发布服务", version="2.0.0", lifespan=lifespan)


@app.exception_handler(ServiceError)
async def handle_service_error(request, exc: ServiceError):
    del request
    body = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details:
        body["error"]["details"] = exc.details
    return JSONResponse(status_code=exc.status, content=body)


def current_user(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "缺少 Bearer 会话")
    return ResearchService().authenticate(authorization[7:])


@app.get("/")
def root():
    return {"service": "考古研究论证与发布服务", "version": "2.0.0"}


@app.get("/api/system/health")
def health():
    db = connection()
    return {"status": "ok", "foreign_keys": db.execute("PRAGMA foreign_keys").fetchone()[0], "journal_mode": db.execute("PRAGMA journal_mode").fetchone()[0]}


@app.post("/api/users", status_code=201)
def create_user(payload: UserCreate):
    return ResearchService().create_user(payload.model_dump())


@app.post("/api/sessions")
def login(payload: LoginRequest):
    return ResearchService().login(payload.username, payload.password)


@app.post("/api/projects", status_code=201)
def create_project(payload: ProjectCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return ResearchService().create_project(payload.model_dump(), user["id"], idempotency_key)


@app.post("/api/projects/{project_id}/members")
def add_member(project_id: int, payload: MemberCreate, user=Depends(current_user)):
    return ResearchService().add_member(project_id, user["id"], payload.user_id, payload.role)


@app.get("/api/audit")
def list_audit(project_id: int | None = Query(default=None), user=Depends(current_user)):
    service = ResearchService()
    if project_id is not None:
        service.require_role(project_id, user["id"], {"owner", "researcher", "reviewer", "viewer"})
        rows = connection().execute("SELECT * FROM audit_events WHERE project_id=? ORDER BY id", (project_id,)).fetchall()
    else:
        rows = connection().execute("SELECT * FROM audit_events WHERE actor_id=? ORDER BY id", (user["id"],)).fetchall()
    return {"data": [dict(row) for row in rows]}


@app.get("/api/audit/verify")
def verify_audit(user=Depends(current_user)):
    del user
    return BaseService().verify_audit_chain()


@app.post("/api/jobs", status_code=202)
def enqueue_job(payload: JobCreate, user=Depends(current_user)):
    return ResearchService().enqueue(payload.model_dump(), user["id"])


@app.post("/api/jobs/claim")
def claim_job(worker_id: str = Query(..., min_length=1)):
    return {"job": ResearchService().claim(worker_id)}


@app.post("/api/jobs/{job_id}/finish")
def finish_job(job_id: int, payload: JobFinish):
    return ResearchService().finish(job_id, payload.worker_id, payload.result)


# ---------- 证据资源 ----------

@app.post("/api/projects/{project_id}/resources", status_code=201)
def create_resource(project_id: int, payload: ResourceCreate, user=Depends(current_user), idempotency_key: str = Header(default="")):
    return EvidenceService().create_resource(project_id, payload.model_dump(), user["id"], idempotency_key)


@app.get("/api/projects/{project_id}/resources")
def list_resources(project_id: int, user=Depends(current_user)):
    return {"data": EvidenceService().list_resources(project_id, user["id"])}


@app.get("/api/resources/{resource_id}")
def get_resource(resource_id: int, user=Depends(current_user)):
    return EvidenceService().get_resource(resource_id, user["id"])


@app.post("/api/resources/{resource_id}/versions", status_code=201)
def add_resource_version(resource_id: int, payload: ResourceVersionCreate, user=Depends(current_user)):
    return EvidenceService().add_version(resource_id, payload.data, user["id"])


@app.post("/api/resources/{resource_id}/withdraw")
def withdraw_resource(resource_id: int, user=Depends(current_user)):
    return EvidenceService().withdraw_resource(resource_id, user["id"])


@app.get("/api/resources/{resource_id}/diff")
def diff_resource_versions(resource_id: int, from_version: int = Query(..., ge=1, alias="from"), to_version: int = Query(..., ge=1, alias="to"), user=Depends(current_user)):
    return EvidenceService().diff_versions(resource_id, from_version, to_version, user["id"])


# ---------- 论点与论据边 ----------

@app.post("/api/projects/{project_id}/claims", status_code=201)
def create_claim(project_id: int, payload: ClaimCreate, user=Depends(current_user)):
    return EvidenceService().create_claim(project_id, payload.model_dump(), user["id"])


@app.get("/api/projects/{project_id}/claims")
def list_claims(project_id: int, affected: bool | None = Query(default=None), user=Depends(current_user)):
    return {"data": EvidenceService().list_claims(project_id, user["id"], affected)}


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: int, user=Depends(current_user)):
    return EvidenceService().get_claim(claim_id, user["id"])


@app.patch("/api/claims/{claim_id}")
def update_claim(claim_id: int, payload: ClaimUpdate, user=Depends(current_user)):
    return EvidenceService().update_claim(claim_id, payload.model_dump(exclude_unset=True), user["id"])


@app.get("/api/claims/{claim_id}/revisions")
def list_claim_revisions(claim_id: int, user=Depends(current_user)):
    return {"data": EvidenceService().claim_revisions(claim_id, user["id"])}


@app.post("/api/claims/{claim_id}/edges", status_code=201)
def create_edge(claim_id: int, payload: EdgeCreate, user=Depends(current_user)):
    return EvidenceService().create_edge(claim_id, payload.model_dump(), user["id"])


@app.post("/api/claims/{claim_id}/resolve")
def resolve_claim(claim_id: int, user=Depends(current_user)):
    return EvidenceService().resolve_claim(claim_id, user["id"])


@app.post("/api/edges/{edge_id}/withdraw")
def withdraw_edge(edge_id: int, user=Depends(current_user)):
    return EvidenceService().withdraw_edge(edge_id, user["id"])


@app.post("/api/edges/{edge_id}/repin")
def repin_edge(edge_id: int, payload: EdgeRepin, user=Depends(current_user)):
    return EvidenceService().repin_edge(edge_id, payload.version, user["id"])


# ---------- 评审策略与论证稿 ----------

@app.post("/api/projects/{project_id}/review-policies", status_code=201)
def create_review_policy(project_id: int, payload: ReviewPolicyCreate, user=Depends(current_user)):
    return PublicationService().create_policy(project_id, payload.model_dump(), user["id"])


@app.get("/api/projects/{project_id}/review-policies")
def list_review_policies(project_id: int, user=Depends(current_user)):
    return {"data": PublicationService().list_policies(project_id, user["id"])}


@app.post("/api/projects/{project_id}/arguments", status_code=201)
def create_argument(project_id: int, payload: ArgumentCreate, user=Depends(current_user)):
    return PublicationService().create_argument(project_id, payload.model_dump(), user["id"])


@app.get("/api/projects/{project_id}/arguments")
def list_arguments(project_id: int, user=Depends(current_user)):
    return {"data": PublicationService().list_arguments(project_id, user["id"])}


@app.get("/api/arguments/{argument_id}")
def get_argument(argument_id: int, user=Depends(current_user)):
    return PublicationService().get_argument(argument_id, user["id"])


@app.put("/api/arguments/{argument_id}")
def update_argument(argument_id: int, payload: ArgumentUpdate, user=Depends(current_user)):
    return PublicationService().update_argument(argument_id, payload.model_dump(exclude_unset=True), user["id"])


@app.post("/api/arguments/{argument_id}/submit")
def submit_argument(argument_id: int, user=Depends(current_user)):
    return PublicationService().submit(argument_id, user["id"])


@app.post("/api/arguments/{argument_id}/reviews", status_code=201)
def review_argument(argument_id: int, payload: ReviewCreate, user=Depends(current_user)):
    return PublicationService().review(argument_id, payload.model_dump(), user["id"])


@app.post("/api/arguments/{argument_id}/publish")
def publish_argument(argument_id: int, user=Depends(current_user)):
    return PublicationService().publish(argument_id, user["id"])


@app.post("/api/arguments/{argument_id}/retract")
def retract_argument(argument_id: int, user=Depends(current_user)):
    return PublicationService().retract(argument_id, user["id"])


@app.get("/api/arguments/{argument_id}/publication")
def get_publication(argument_id: int, user=Depends(current_user)):
    return PublicationService().get_publication(argument_id, user["id"])


@app.get("/api/arguments/{argument_id}/diff")
def diff_argument_revisions(argument_id: int, from_revision: int = Query(..., ge=1, alias="from"), to_revision: int = Query(..., ge=1, alias="to"), user=Depends(current_user)):
    return PublicationService().diff_revisions(argument_id, from_revision, to_revision, user["id"])
