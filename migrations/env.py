"""Alembic environment.

Three settings here are load-bearing and are much cheaper to get right in the
first revision than to retrofit later:

  * `version_table_schema="ai"` keeps Alembic's own bookkeeping table out of
    `public`, which the product backend owns.
  * `include_schemas=True` makes autogenerate look at non-default schemas at all.
  * `include_object` filters everything outside `ai`, so autogenerate never
    proposes dropping the product domain's tables when they share the database.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from app.config import settings
from app.db.models import SCHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ in ("table", "index"):
        return getattr(obj, "schema", None) == SCHEMA
    return True


_common = dict(
    target_metadata=target_metadata,
    version_table="alembic_version",
    version_table_schema=SCHEMA,
    include_schemas=True,
    include_object=include_object,
    compare_type=True,
    compare_server_default=True,
)


def run_migrations_offline() -> None:
    context.configure(
        url=settings().database_migrate_url, literal_binds=True, **_common
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(settings().database_migrate_url, future=True)
    with engine.connect() as connection:
        context.configure(connection=connection, **_common)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
