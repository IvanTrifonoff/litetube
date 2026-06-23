"""Pytest fixtures for Litetube backend tests."""

import os
import sys

import pytest

# Set required env vars BEFORE importing any litetube modules.
# auth.py checks JWT_SECRET at module load and raises if absent/too-short.
os.environ["JWT_SECRET"] = "a" * 64  # 64-char test secret
os.environ["JWT_EXPIRY_HOURS"] = "24"
os.environ["TRIAL_DURATION_DAYS"] = "3"
os.environ["BILLING_PROVIDER"] = "mock"
os.environ["LITETUBE_ENV"] = "development"
os.environ["RATE_LIMIT_PER_IP_PER_MIN"] = "9999"
os.environ["DEVICE_CLAIM_TTL_SEC"] = "600"
os.environ["DEVICE_POLL_MAX_SEC"] = "5"
os.environ["DEVICE_POLL_TICK_MS"] = "100"
os.environ["DEVICE_START_PER_IP_PER_MIN"] = "100"
os.environ["PROXY_HOST"] = "82.202.141.81"

# Ensure the backend is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "api"))


@pytest.fixture(autouse=True)
async def _seed_proxy_pool(test_db):
    """Insert a dummy proxy so signup's allocate_proxy_for_user succeeds."""
    from litetube import db as db_mod
    # Foreign keys are ON — create a dummy user so proxy assignment works.
    await db_mod.conn().execute(
        "INSERT INTO users(email,password_hash,role,status,trial_started_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("seed@test.local", "$2b$12$...", "client", "trial",
         await db_mod.now(), await db_mod.now(), await db_mod.now()))
    await db_mod.conn().execute(
        "INSERT INTO proxies(uid,host,port,type,auth_token,is_active,is_alive,owner_user_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("dummy-001", "127.0.0.1", 11001, "socks5http", "dummy-token", 1, 1, 1,
         await db_mod.now(), await db_mod.now()))


@pytest.fixture
def jwt_secret():
    return os.environ["JWT_SECRET"]


@pytest.fixture
async def test_db():
    """Temp-file SQLite database with all migrations applied."""
    import tempfile
    from litetube import db

    # delete=False so SQLite can open the file independently; cleaned up manually.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    path = tmp.name
    tmp.close()
    try:
        await db.init(path)
        yield
    finally:
        os.unlink(path)


@pytest.fixture
async def test_client(test_db):
    """Async HTTP client against the FastAPI app (ASGI transport, no socket)."""
    from httpx import ASGITransport, AsyncClient
    from litetube.main import app

    # The app's lifespan calls db.init() but we already initialised via test_db.
    # Override the lifespan to skip double-init.
    original_lifespan = app.router.lifespan_context
    app.router.lifespan_context = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.router.lifespan_context = original_lifespan
