from logging.config import fileConfig

from alembic import context

from app.db.base import Base
from app.db.session import engine, build_database_url
from app.db.models import (
    Conversation,
    ConversationMessage,
    Memory,
    UserSettings,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# SQLAlchemy ORM metadata
target_metadata = Base.metadata


# Only include application-owned tables.
# Never generate destructive operations for reflected objects
# that are not represented in the ORM metadata.
def include_object(object, name, type_, reflected, compare_to):

    if type_ == "table":
        schema = object.schema

        # Never manage Supabase Auth or other schemas.
        if schema not in (None, "public"):
            return False

        # Only manage our application's tables.
        if not name.startswith("pa_"):
            return False

    # Preserve database objects missing from ORM metadata.
    if reflected and compare_to is None:
        return False

    return True


def run_migrations_offline() -> None:
    """
    Generate SQL without opening a database connection.
    """

    context.configure(
        url=build_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table="pa_alembic_version",
        include_object=include_object,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations using the application's SQLAlchemy engine.
    """

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table="pa_alembic_version",
            include_object=include_object,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()