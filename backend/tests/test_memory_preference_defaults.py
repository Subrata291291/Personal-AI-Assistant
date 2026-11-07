from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models.user_settings import UserSettings
from app.memory.memory_pipeline import MemoryPipeline
from app.repositories.user_settings_repository import UserSettingsRepository
from app.memory.memory_decision import MemoryDecision


@pytest.mark.parametrize(
    ("memory_type", "action"),
    (
        ("long_term", "CREATE"),
        ("semantic", "CREATE"),
        ("episodic", "UPDATE"),
        ("procedural", "IGNORE"),
    ),
)
def test_memory_decision_schema_accepts_supported_actions_and_types(
    memory_type,
    action,
):
    decision = MemoryDecision(
        action=action,
        memory_type=memory_type,
        target_memory_id=None,
        proposed_content="User is learning LangGraph.",
        confidence=0.95,
        importance=7,
        reason="Meaningful new information.",
    )

    assert decision.action == action
    assert decision.memory_type == memory_type


def test_new_settings_rows_enable_automatic_memory_without_overwriting_existing_choice():
    user_id = uuid4()
    existing_settings = SimpleNamespace(memory_enabled=False)
    session = Mock()
    repository = UserSettingsRepository(session)
    repository.get_settings = Mock(return_value=existing_settings)

    settings = repository.get_or_create_settings(user_id)

    statement = session.execute.call_args.args[0]
    insert_values = statement.compile(
        dialect=postgresql.dialect(),
    ).params
    assert insert_values["memory_enabled"] is True
    assert "ON CONFLICT (user_id) DO NOTHING" in str(
        statement.compile(dialect=postgresql.dialect())
    )
    assert settings.memory_enabled is False
    assert UserSettings.memory_enabled.default.arg is True


def test_disabled_preference_skips_memory_extraction():
    pipeline = object.__new__(MemoryPipeline)
    pipeline.classifier = Mock()

    result = pipeline.process(
        user_id=uuid4(),
        content="I prefer concise answers.",
        memory_enabled=False,
    )

    assert result.status == "disabled"
    pipeline.classifier.classify.assert_not_called()


def test_memory_opt_out_and_sensitive_content_skip_extraction():
    pipeline = object.__new__(MemoryPipeline)
    pipeline.classifier = Mock()
    user_id = uuid4()

    opt_out = pipeline.process(
        user_id=user_id,
        content="Don't remember this: I am changing jobs.",
        memory_enabled=True,
    )
    sensitive = pipeline.process(
        user_id=user_id,
        content="My API key is sk-12345678901234567890",
        memory_enabled=True,
    )

    assert opt_out.status == "rejected"
    assert sensitive.status == "rejected"
    pipeline.classifier.classify.assert_not_called()


def test_enabling_memory_invokes_extraction():
    pipeline = object.__new__(MemoryPipeline)
    pipeline.classifier = Mock(memories=[])
    pipeline.classifier.classify.return_value = SimpleNamespace(memories=[])

    result = pipeline.process(
        user_id=uuid4(),
        content="I prefer concise answers.",
        memory_enabled=True,
    )

    assert result.status == "rejected"
    pipeline.classifier.classify.assert_called_once_with(
        "I prefer concise answers.",
    )
