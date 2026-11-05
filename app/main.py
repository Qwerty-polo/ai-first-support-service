from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory


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

    @application.get("/health", tags=["operations"])
    async def health() -> dict[str, str]:
        # Liveness stays available during a DB outage; Compose checks PostgreSQL separately.
        return {"status": "ok"}

    return application


app = create_app()
