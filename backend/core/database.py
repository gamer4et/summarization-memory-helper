"""
SQLAlchemy engine, session factory, declarative Base, and FastAPI dependency.
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from backend.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

connect_args: dict = {}
if settings.database.url.startswith("sqlite"):
    # Enable WAL mode and foreign keys for SQLite.
    connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database.url,
    connect_args=connect_args,
    echo=settings.debug,
)

if settings.database.url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):  # noqa: ANN001
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

SessionLocal: sessionmaker[Session] = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


# ---------------------------------------------------------------------------
# Declarative Base (imported by models)
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_all_tables() -> None:
    """Create all tables declared on Base.  Call once at startup."""
    # Import models so their metadata is registered before create_all.
    import backend.models.orm  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_lightweight_schema_updates()
    logger.info("Database tables ensured.")


def _ensure_lightweight_schema_updates() -> None:
    """Apply simple additive schema updates for deployments without migrations."""
    inspector = inspect(engine)
    if "chapter_test_options" not in inspector.get_table_names():
        return

    option_columns = {column["name"] for column in inspector.get_columns("chapter_test_options")}
    if "wrong_explanation" not in option_columns:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE chapter_test_options ADD COLUMN wrong_explanation TEXT"))
        logger.info("Added missing chapter_test_options.wrong_explanation column.")


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def get_db() -> Generator[Session, None, None]:
    """
    Yield a SQLAlchemy session and always close it afterward.

    Usage::

        @router.get("/example")
        def example(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """
    Context-manager version for use outside FastAPI request handlers
    (e.g., background tasks).
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
