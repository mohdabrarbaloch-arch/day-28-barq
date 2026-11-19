"""Database engine, session factory and declarative base.

Written to work unchanged against PostgreSQL (production, via docker-compose)
and SQLite (local development, zero setup). The only place that difference
leaks is the connect_args branch below.
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    """Declarative base shared by every model."""


def _engine_kwargs() -> dict:
    kwargs: dict = {
        "echo": settings.db_echo,
        "pool_pre_ping": True,
        "future": True,
    }
    if settings.database_url.startswith("sqlite"):
        # A single-file DB shared across threads: FastAPI runs sync endpoints in
        # a threadpool, and SQLite forbids cross-thread use by default.
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = __import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool
    else:
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
        kwargs["pool_recycle"] = 1800
    return kwargs


engine: Engine = create_engine(settings.database_url, **_engine_kwargs())


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):
        """Turn on foreign keys and WAL.

        SQLite ignores FOREIGN KEY constraints unless asked, and WAL lets a
        reader run while a writer holds the lock.
        """
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables. Real deployments use migrations; this is the SQLite path."""
    from app import models  # noqa: F401  (registers mappers)

    Base.metadata.create_all(bind=engine)
