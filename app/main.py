from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html import escape

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.knowledge import ARTICLES
from app.operator import router as operator_router
from app.webhooks import router as webhooks_router


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(config)
        application.state.session_factory = create_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    application = FastAPI(title="Support Draft Service", version="0.1.0", lifespan=lifespan)
    application.state.settings = config
    application.include_router(webhooks_router)
    application.include_router(operator_router)

    @application.get("/", include_in_schema=False)
    async def home() -> RedirectResponse:
        return RedirectResponse("/operator/")

    @application.get("/kb/{article_id}", response_class=HTMLResponse)
    async def article_page(article_id: str) -> str:
        article = next((a for a in ARTICLES if a.id == article_id), None)
        if article is None:
            raise HTTPException(404, "Article not found")
        return (
            "<!doctype html><html lang='uk'><meta charset='utf-8'>"
            f"<title>{escape(article.title)}</title><main><h1>{escape(article.title)}</h1>"
            f"<p>{escape(article.body)}</p></main></html>"
        )

    @application.get("/health", tags=["operations"])
    async def health() -> dict[str, str]:
        # Liveness stays available during a DB outage; Compose checks PostgreSQL separately.
        return {"status": "ok"}

    return application


app = create_app()
