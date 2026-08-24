"""Alembic environment for the product domain.

The mirror image of the AI service's `migrations/env.py`, and the filters
matter for the same reason: the two domains may share one database, so
autogenerate must be prevented from ever proposing a drop of the other side's
tables.

  * `version_table="alembic_version_product"` — a distinct bookkeeping table.
    Sharing one with the AI service would make each migration run think the
    other's revisions were unknown heads.
  * `include_object` keeps `ai.*` invisible here.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

from product.config import settings
from product.db.models import SCHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ in ("table", "index"):
        schema = getattr(obj, "schema", None)
        # `public` is the default schema, so SQLAlchemy reports it as None on
        # reflected objects and as "public" on ours. Accept both, and nothing
        # else — in particular never `ai`.
        return schema in (None, SCHEMA)
    return True


_common = dict(
    target_metadata=target_metadata,
    version_table="alembic_version_product",
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
