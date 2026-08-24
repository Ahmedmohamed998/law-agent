"""
Engine, session factory, and the one place the tenant is bound to a connection.

Every unit of work runs inside `scoped_session(principal)`, which opens a
transaction and sets `ai.org_id` on it before any statement runs. Row-level
security reads that setting, so a query that forgets its tenant filter returns
nothing rather than another firm's conversations.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as SASession
from sqlalchemy.orm import sessionmaker

from app.config import settings


@dataclass(frozen=True, slots=True)
class Principal:
    """The verified caller. Only ever built from a validated token."""

    user_id: str
    organization_id: str | None = None
    role: str = "client"
    anonymous: bool = False

    @property
    def org_key(self) -> str:
        """RLS comparison key. '' for users with no organization."""
        return self.organization_id or ""


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
def scoped_session(principal: Principal) -> Iterator[SASession]:
    """A transaction bound to one tenant. Commits on success, rolls back on error."""
    with session_factory()() as db:
        with db.begin():
            # SET LOCAL, so the value dies with the transaction and cannot leak
            # to the next request that borrows this pooled connection.
            db.execute(
                text("SELECT set_config('ai.org_id', :org, true)"),
                {"org": principal.org_key},
            )
            yield db


_admin_factory: sessionmaker[SASession] | None = None


@contextmanager
def admin_session() -> Iterator[SASession]:
    """Cross-tenant session for maintenance paths (erasure, backfills).

    Connects as the owner role, which bypasses row-level security — that is the
    point, and it is why the request path must never use this. Deliberately a
    separate engine so the app's pool cannot accidentally hand out an
    unrestricted connection.
    """
    global _admin_factory
    if _admin_factory is None:
        admin_engine = create_engine(
            settings().database_migrate_url, pool_size=2, pool_pre_ping=True, future=True
        )
        _admin_factory = sessionmaker(
            bind=admin_engine, expire_on_commit=False, future=True
        )
    with _admin_factory() as db:
        with db.begin():
            yield db
