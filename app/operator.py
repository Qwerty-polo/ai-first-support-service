import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models import HelpdeskEvent, Job
from app.webhooks import get_session_factory

router = APIRouter(prefix="/operator")
basic = HTTPBasic()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
Decision = Literal["approve", "reject"]
Sessions = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


def require_operator(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(basic)],
) -> Settings:
    settings: Settings = request.app.state.settings
    username_ok = secrets.compare_digest(
        credentials.username.encode(), settings.operator_username.encode()
    )
    password = settings.operator_password.get_secret_value()
    password_ok = secrets.compare_digest(credentials.password.encode(), password.encode())
    if not username_ok or not password_ok or not password:
        raise HTTPException(
            401, "Invalid operator credentials", headers={"WWW-Authenticate": "Basic"}
        )
    return settings


Operator = Annotated[Settings, Depends(require_operator)]


def csrf_token(settings: Settings, job_id: UUID, action: str) -> str:
    # Bound to job/action and a secret, so a cross-origin form cannot forge a decision.
    return hmac.new(
        settings.operator_password.get_secret_value().encode(),
        f"operator-review:{job_id}:{action}".encode(),
        hashlib.sha256,
    ).hexdigest()


async def review_job(
    sessions: async_sessionmaker[AsyncSession],
    job_id: UUID,
    action: Decision,
) -> None:
    decision = "approved" if action == "approve" else "rejected"
    async with sessions.begin() as session:
        # Lock the event shared by competing approve/reject requests until delivery is queued.
        event = await session.scalar(
            select(HelpdeskEvent)
            .join(Job)
            .where(
                Job.id == job_id,
                Job.kind == "generation",
            )
            .with_for_update(of=HelpdeskEvent)
        )
        if event is None:
            raise HTTPException(404, "Draft not found")
        if event.decision == decision:
            return
        if event.decision != "pending":
            raise HTTPException(409, "Draft already reviewed")
        generation = await session.get(Job, job_id)
        if generation.state != "succeeded" or not generation.result:
            raise HTTPException(409, "Draft is not ready")
        message = generation.result.get("message")
        if not isinstance(message, str) or not message.strip():
            raise HTTPException(409, "Draft has no message")
        event.decision = decision
        event.reviewed_at = func.clock_timestamp()
        if action == "approve":
            # Freeze the approved text; unique(event_id, kind) is the final duplicate guard.
            session.add(Job(event_id=event.event_id, kind="delivery", payload={"message": message}))


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    settings: Operator,
    sessions: Sessions,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(HelpdeskEvent, Job)
                .join(Job)
                .where(
                    Job.kind == "generation",
                )
                .order_by(HelpdeskEvent.created_at.desc(), HelpdeskEvent.event_id)
                .offset((page - 1) * 25)
                .limit(26)
            )
        ).all()
        event_ids = [event.event_id for event, _ in rows[:25]]
        deliveries = (
            (
                await session.scalars(
                    select(Job).where(
                        Job.kind == "delivery",
                        Job.event_id.in_(event_ids),
                    )
                )
            ).all()
            if event_ids
            else []
        )
    response = templates.TemplateResponse(
        request=request,
        name="operator.html",
        context={
            "rows": rows[:25],
            "deliveries": {j.event_id: j for j in deliveries},
            "page": page,
            "has_next": len(rows) > 25,
            "csrf": lambda job, action: csrf_token(settings, job, action),
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"
    )
    return response


@router.post("/jobs/{job_id}/{action}")
async def decide(
    job_id: UUID,
    action: Decision,
    settings: Operator,
    sessions: Sessions,
    csrf: Annotated[str, Form()],
) -> RedirectResponse:
    if not secrets.compare_digest(csrf, csrf_token(settings, job_id, action)):
        raise HTTPException(403, "Invalid CSRF token")
    try:
        await review_job(sessions, job_id, action)
    except (SQLAlchemyError, OSError, TimeoutError):
        # A failed commit must not look like an approval; never expose SQL parameters.
        raise HTTPException(
            503, "Review unavailable; retry", headers={"Retry-After": "1"}
        ) from None
    return RedirectResponse("/operator/", status_code=303)
