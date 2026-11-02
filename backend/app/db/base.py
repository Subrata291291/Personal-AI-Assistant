
from sqlalchemy import Column, Table
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Base class for all application ORM models.
    """

    pass


# Local metadata reference to Supabase's existing Auth table.
# This does NOT create or modify the actual database table.
supabase_users = Table(
    "users",
    Base.metadata,
    Column(
        "id",
        UUID(as_uuid=True),
        primary_key=True,
    ),
    schema="auth",
)
