"""Engine and session factory.

Simpler than the AI service's equivalent on purpose: there is no row-level
security here and no per-transaction tenant binding, because this service IS
the source of truth for who a tenant is. Authorization is explicit in each
handler instead.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker

from product.config import settings

_engine = None
_factory: sessionmaker[SASession] | None = None


def engine():
    global _engine, _factory
    if _engine is None:
        s = settings()
        _engine = create_engine(
            s.database_url,
            pool_size=s.db_pool_size,
            max_overflow=s.db_max_overflow,
            pool_pre_ping=True,
            echo=s.db_echo,
            future=True,
        )
        _factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def session_factory() -> sessionmaker[SASession]:
    engine()
    assert _factory is not None
    return _factory


@contextmanager
def tx() -> Iterator[SASession]:
    """One unit of work. Commits on success, rolls back on any exception."""
    with session_factory()() as db:
        with db.begin():
            yield db
