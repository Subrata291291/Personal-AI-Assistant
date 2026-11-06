import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    insert,
)
from sqlalchemy.exc import IntegrityError

from app.db.models.oauth_state import OAuthState


def test_oauth_state_service_constraint_matches_migration():
    constraints = [
        constraint
        for constraint in OAuthState.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_pa_oauth_states_service"
    ]

    assert len(constraints) == 1
    assert str(constraints[0].sqltext) == (
        "service IS NULL OR service IN ('gmail', 'calendar', 'calendar_write')"
    )
    assert OAuthState.__table__.c.service.type.length == 50
    assert OAuthState.__table__.c.service.nullable


def test_oauth_state_service_constraint_accepts_allowed_values_and_rejects_others():
    constraint = next(
        constraint
        for constraint in OAuthState.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_pa_oauth_states_service"
    )
    metadata = MetaData()
    table = Table(
        "oauth_state_service_constraint_test",
        metadata,
        Column("service", String(50), nullable=True),
        CheckConstraint(str(constraint.sqltext), name=constraint.name),
    )
    engine = create_engine("sqlite://")
    try:
        metadata.create_all(engine)

        with engine.begin() as connection:
            connection.execute(
                insert(table),
                [{"service": value} for value in (None, "gmail", "calendar", "calendar_write")],
            )

        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    insert(table).values(service="drive"),
                )
    finally:
        engine.dispose()
