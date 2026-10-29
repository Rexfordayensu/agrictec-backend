"""The database engine and the per-request session."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    """One session per request. Services commit their own work; anything uncommitted (an error
    half-way through a change) is rolled back here, so a failed write never leaves a trace."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
