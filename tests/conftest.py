import asyncio
import os
import sys
from collections.abc import AsyncIterator
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def database_url() -> AsyncIterator[str]:
    admin_url = os.getenv("TEST_DATABASE_URL")
    if not admin_url:
        pytest.skip(
            "Set TEST_DATABASE_URL to a PostgreSQL admin DSN; tests create their own database"
        )
    url = make_url(admin_url)
    admin = await asyncpg.connect(url.set(drivername="postgresql").render_as_string(False))
    # Only this randomly named database is created/dropped; never clear the supplied database.
    database_name = "support_test_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{database_name}"')
    test_url = url.set(drivername="postgresql+asyncpg", database=database_name).render_as_string(
        False
    )
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "alembic",
            "upgrade",
            "head",
            env={**os.environ, "DATABASE_URL": test_url},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        assert process.returncode == 0, (stdout + stderr).decode(errors="replace")
        yield test_url
    finally:
        await admin.execute(f'DROP DATABASE "{database_name}" WITH (FORCE)')
        await admin.close()


@pytest_asyncio.fixture(loop_scope="module")
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(database_url)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE jobs, helpdesk_events, llm_admissions"))
        await connection.execute(text("UPDATE llm_policy SET concurrency=4"))
    try:
        yield engine
    finally:
        await engine.dispose()
