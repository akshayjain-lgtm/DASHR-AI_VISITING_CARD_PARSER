"""
Shared pytest fixtures for apps/api tests.

DB strategy (documented per task instructions):
- We use a REAL Postgres database, `dashr_test`, on the same Postgres server
  the project already runs via `infra/docker-compose.yml` (service `postgres`,
  user/password `dashr`/`dashr`, port 5432) — NOT SQLite. This schema relies on
  Postgres-only features (partial unique indexes with `postgresql_where`,
  `gen_random_uuid()` server defaults, `TIMESTAMPTZ`) that SQLite cannot
  reproduce, so a SQLite-based test DB would silently fail to enforce the
  exact constraints (e.g. `uq_users_phone_no_verified`) this feature depends
  on for cross-account phone-reuse behavior.
- We use a SEPARATE database (`dashr_test`) rather than reusing the dev `dashr`
  database, so test runs can never truncate or corrupt local dev data. The
  `DATABASE_URL` environment variable is forced (not merely defaulted) to the
  test database before any `app.*` module is imported, so the application's
  own engine/session (`app.db.session.SessionLocal`) transparently points at
  `dashr_test` for the whole test session — no `get_db` dependency override
  needed.
- Schema is created by running the project's real Alembic migrations
  (`0001`..`0004`) against `dashr_test` once per test session — this is the
  same path `alembic upgrade head` takes in the Definition of Done, so the
  tests exercise the real partial-unique-index/constraint behavior, not a
  hand-rolled approximation of it.
- Isolation between tests is via TRUNCATE (not per-test transaction/SAVEPOINT
  rollback): the app's own request-scoped sessions (`get_db`) commit inside
  request handlers, so an outer-transaction/rollback strategy would require
  wiring a SAVEPOINT-aware session into `get_db` and asserting we understand
  exactly when the app commits — which would mean reading the implementation
  to write tests, the one thing we're told not to do. TRUNCATE before every
  test is implementation-agnostic and works no matter how/when the app commits.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- 1. Force test environment variables BEFORE any `app.*` import -------
# This must happen at module import time (not inside a fixture) so it runs
# before pytest imports any test module that does `from app.main import app`.

API_ROOT = Path(__file__).resolve().parent.parent  # apps/api
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://dashr:dashr@localhost:5432/dashr_test",
)

# Force (never merely default) DATABASE_URL so a test run can never end up
# pointed at the dev/prod database, regardless of what's already exported.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("JWT_SECRET", "pytest-only-secret-do-not-use-elsewhere")
os.environ.setdefault("JWT_EXPIRE_MINUTES", "60")
os.environ.setdefault("CORS_ORIGINS", "http://localhost:3000")
os.environ.setdefault("OTP_EXPIRE_MINUTES", "10")
os.environ.setdefault("OTP_MAX_ATTEMPTS", "5")
os.environ.setdefault("OTP_RESEND_COOLDOWN_SECONDS", "30")
os.environ.setdefault("COOKIE_SECURE", "false")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

# Safe to import now: DATABASE_URL is already pinned to the test DB.
from app.db.session import engine as app_engine  # noqa: E402
from app.deps import get_otp_provider  # noqa: E402
from app.main import app  # noqa: E402

ADMIN_DATABASE_URL = "postgresql+psycopg://dashr:dashr@localhost:5432/postgres"
TEST_DB_NAME = "dashr_test"


def _ensure_test_database_exists() -> None:
    admin_engine = create_engine(ADMIN_DATABASE_URL, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": TEST_DB_NAME},
            ).scalar()
            if not exists:
                # Database name is a fixed constant above, not user input;
                # CREATE DATABASE cannot be parameterized in Postgres.
                conn.execute(text(f'CREATE DATABASE "{TEST_DB_NAME}"'))
    finally:
        admin_engine.dispose()


def _run_migrations() -> None:
    alembic_ini = API_ROOT / "alembic.ini"
    cfg = Config(str(alembic_ini))
    # Force an absolute script_location so this works regardless of the
    # cwd pytest was invoked from.
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _test_database() -> None:
    """Ensure `dashr_test` exists and is migrated to head, once per session."""
    _ensure_test_database_exists()
    _run_migrations()


TestSessionLocal = sessionmaker(bind=app_engine, autoflush=False, autocommit=False)


@pytest.fixture(autouse=True)
def _clean_tables() -> None:
    """Truncate feature-relevant tables before every test for isolation.

    Runs before each test (not after) so a prior crashed run never leaves
    stale state for the next test either.
    """
    with app_engine.begin() as conn:
        conn.execute(text("TRUNCATE TABLE phone_otp_verifications, users CASCADE"))
    yield


@pytest.fixture
def db_session():
    """A direct DB session for asserting side effects / seeding time-based state.

    Used only to (a) read back rows the API created, to confirm DB side
    effects, and (b) backdate `created_at`/`expires_at` columns that the spec
    explicitly documents (`phone_otp_verifications.created_at`, `.expires_at`)
    to deterministically simulate elapsed time without `time.sleep()`.
    """
    session: Session = TestSessionLocal()
    try:
        yield session
    finally:
        session.close()


class FakeOtpProvider:
    """Captures OTP codes instead of sending real SMS.

    `send(phone_no, code)` matches the `OtpProvider` protocol the spec
    defines in `services/otp_provider.py`. We record every send in order so
    tests can retrieve exactly which code was sent for a given phone number,
    even across multiple signups/resends (e.g. two different accounts
    in-flight sharing the same unverified phone number).
    """

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, phone_no: str, code: str) -> None:
        self.sent.append((phone_no, code))

    def latest_code_for(self, phone_no: str) -> str:
        for sent_phone, code in reversed(self.sent):
            if sent_phone == phone_no:
                return code
        raise AssertionError(f"No OTP code was ever sent to {phone_no!r}")

    def count_sent(self, phone_no: str) -> int:
        return sum(1 for sent_phone, _ in self.sent if sent_phone == phone_no)


@pytest.fixture
def fake_otp_provider() -> FakeOtpProvider:
    return FakeOtpProvider()


@pytest.fixture
def client(fake_otp_provider: FakeOtpProvider):
    """A fresh TestClient per test, with the OTP provider mocked.

    Never hits a real SMS/OTP API — `get_otp_provider` is overridden for
    every single test, per the task's instruction to mock it for ALL tests.
    """
    app.dependency_overrides[get_otp_provider] = lambda: fake_otp_provider
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_otp_provider, None)
