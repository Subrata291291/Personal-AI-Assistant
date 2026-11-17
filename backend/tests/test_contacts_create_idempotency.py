import uuid
from datetime import datetime, timezone

import pytest

from app.schemas.action_approval import ActionApprovalProposal
from app.services.contacts_idempotency import (
    CONTACTS_CREATE_IDEMPOTENCY_NORMALIZATION_VERSION,
    build_contacts_create_idempotency_context,
    contacts_create_payload_fingerprint,
    derive_contacts_tenant_id,
    normalize_contacts_create_parameters,
    validate_contacts_create_idempotency_key,
)


USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000111")
CONNECTION_ID = uuid.UUID("00000000-0000-0000-0000-000000000222")
OTHER_CONNECTION_ID = uuid.UUID("00000000-0000-0000-0000-000000000333")


def _proposal(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        proposal_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        source_message_id=uuid.uuid4(),
        user_id=USER_ID,
        conversation_id=uuid.uuid4(),
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        parameters={"display_name": "Ada Lovelace", "email": "ada@example.com"},
        created_at=now,
        expires_at=now,
        status="pending",
        approved_at=None,
        execution_started_at=None,
        completed_at=None,
        result=None,
        execution_available=True,
        integrity_schema_version=2,
        payload_fingerprint="f" * 64,
    )
    base.update(overrides)
    return ActionApprovalProposal(**base)


def test_validate_contacts_create_idempotency_key_requires_value():
    with pytest.raises(ValueError):
        validate_contacts_create_idempotency_key(None)
    with pytest.raises(ValueError):
        validate_contacts_create_idempotency_key("   ")


def test_validate_contacts_create_idempotency_key_rejects_invalid_format_and_length():
    with pytest.raises(ValueError):
        validate_contacts_create_idempotency_key("bad key with spaces")
    with pytest.raises(ValueError):
        validate_contacts_create_idempotency_key("x" * 129)
    assert validate_contacts_create_idempotency_key("  abc-123_uuid:v1  ") == "abc-123_uuid:v1"


def test_tenant_identity_derived_server_side_from_authenticated_user():
    assert derive_contacts_tenant_id(USER_ID) == USER_ID


def test_normalize_contacts_create_parameters_uses_proposal_validation():
    params = normalize_contacts_create_parameters(
        {"display_name": "  Ada  ", "email": "ADA@EXAMPLE.COM", "phone": " +1 555 0100 "}
    )
    # Normalization must match existing proposal validation (deterministic).
    expected = normalize_contacts_create_parameters(
        {"display_name": "  Ada  ", "email": "ADA@EXAMPLE.COM", "phone": " +1 555 0100 "}
    )
    assert params == expected
    assert "display_name" in params and "email" in params and "phone" in params


def test_same_normalized_payload_produces_same_fingerprint():
    params_a = {"display_name": "Ada", "email": "ada@example.com", "organization": "Acme"}
    params_b = {"organization": "Acme", "email": "ada@example.com", "display_name": "Ada"}
    a = normalize_contacts_create_parameters(params_a)
    b = normalize_contacts_create_parameters(params_b)
    assert a == b
    fp1 = contacts_create_payload_fingerprint(
        tenant_id=USER_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        normalized_parameters=a,
    )
    fp2 = contacts_create_payload_fingerprint(
        tenant_id=USER_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        normalized_parameters=b,
    )
    assert fp1 == fp2
    assert len(fp1) == 64

def test_different_payload_connection_or_version_produce_different_fingerprint():
    base = normalize_contacts_create_parameters({"display_name": "Ada", "email": "ada@example.com"})
    different = normalize_contacts_create_parameters({"display_name": "Ada", "email": "ada2@example.com"})
    fp_base = contacts_create_payload_fingerprint(
        tenant_id=USER_ID, user_id=USER_ID, connection_id=CONNECTION_ID,
        action_type="contacts.create_contact", normalized_parameters=base,
    )
    fp_payload = contacts_create_payload_fingerprint(
        tenant_id=USER_ID, user_id=USER_ID, connection_id=CONNECTION_ID,
        action_type="contacts.create_contact", normalized_parameters=different,
    )
    fp_conn = contacts_create_payload_fingerprint(
        tenant_id=USER_ID, user_id=USER_ID, connection_id=OTHER_CONNECTION_ID,
        action_type="contacts.create_contact", normalized_parameters=base,
    )
    fp_version = contacts_create_payload_fingerprint(
        tenant_id=USER_ID, user_id=USER_ID, connection_id=CONNECTION_ID,
        action_type="contacts.create_contact", normalized_parameters=base,
        normalization_version=CONTACTS_CREATE_IDEMPOTENCY_NORMALIZATION_VERSION + 1,
    )
    assert fp_base != fp_payload
    assert fp_base != fp_conn
    assert fp_base != fp_version


def test_build_context_uses_server_derived_scope_and_ignores_client_fingerprint():
    ctx = build_contacts_create_idempotency_context(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        idempotency_key="intent-1",
        parameters={"display_name": "Ada", "email": "ADA@EXAMPLE.COM", "payload_fingerprint": "client"},
    )
    assert ctx.tenant_id == USER_ID
    assert ctx.user_id == USER_ID
    assert ctx.connection_id == CONNECTION_ID
    assert ctx.normalized_parameters == normalize_contacts_create_parameters({"display_name": "Ada", "email": "ADA@EXAMPLE.COM"})
    assert "payload_fingerprint" not in ctx.normalized_parameters
    assert ctx.payload_fingerprint == contacts_create_payload_fingerprint(
        tenant_id=USER_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        normalized_parameters=ctx.normalized_parameters,
    )


def test_build_context_rejects_non_create_action():
    with pytest.raises(ValueError):
        build_contacts_create_idempotency_context(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            action_type="contacts.update_contact",
            idempotency_key="intent-3",
            parameters={"contact_id": "people/c1"},
        )


class _FakeMappings:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def mappings(self):
        return _FakeMappings(self._row)


class _FakeConnection:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []

    def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params))
        return _FakeResult(self.rows.pop(0) if self.rows else None)


class _FakeBegin:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeBind:
    def __init__(self, conn):
        self.conn = conn

    def begin(self):
        return _FakeBegin(self.conn)


class _FakeDB:
    def __init__(self, conn):
        self._conn = conn

    def get_bind(self):
        return _FakeBind(self._conn)


def test_idempotency_service_creates_ledger_and_proposal_on_same_connection(monkeypatch):
    from app.services.contacts_idempotency_service import ContactsIdempotencyService

    conn = _FakeConnection(rows=[{"id": uuid.uuid4(), "proposal_id": uuid.uuid4(), "payload_fingerprint": "x", "normalization_version": 1}])
    db = _FakeDB(conn)
    service = ContactsIdempotencyService(db)

    monkeypatch.setattr("app.services.contacts_idempotency_service.settings.CONTACTS_CREATE_ENABLED", True)
    captured = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _proposal(proposal_id=kwargs["proposal_id"], parameters=kwargs["parameters"])

    monkeypatch.setattr(service.approval_service, "create_standalone_contacts_proposal", fake_create)

    ctx = build_contacts_create_idempotency_context(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        idempotency_key="same-key",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
    )
    result = service.create_contacts_create_proposal(user_id=USER_ID, connection_id=CONNECTION_ID, context=ctx)

    assert result.replayed is False
    assert captured["db_connection"] is conn
    assert "INSERT INTO public.pa_contacts_action_idempotency" in conn.statements[0][0]
    assert conn.statements[0][1]["idempotency_key"] == "same-key"
    assert conn.statements[0][1]["payload_fingerprint"] == ctx.payload_fingerprint
    assert conn.statements[0][1]["proposal_id"] == str(captured["proposal_id"])


def test_idempotency_service_replays_existing_same_payload_without_execution(monkeypatch):
    from app.services.contacts_idempotency_service import ContactsIdempotencyService

    existing_proposal_id = uuid.uuid4()
    ctx = build_contacts_create_idempotency_context(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        idempotency_key="same-key",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
    )
    conn = _FakeConnection(rows=[
        None,  # insert conflict
        {
            "id": uuid.uuid4(),
            "proposal_id": existing_proposal_id,
            "payload_fingerprint": ctx.payload_fingerprint,
            "normalization_version": ctx.normalization_version,
        },
    ])
    db = _FakeDB(conn)
    service = ContactsIdempotencyService(db)
    monkeypatch.setattr("app.services.contacts_idempotency_service.settings.CONTACTS_CREATE_ENABLED", True)

    called = {"create": 0, "get": 0}

    def fake_create(**kwargs):
        called["create"] += 1
        raise AssertionError("must not create proposal on replay")

    def fake_get(user_id, proposal_id):
        called["get"] += 1
        assert user_id == USER_ID
        assert proposal_id == existing_proposal_id
        return _proposal(proposal_id=existing_proposal_id, status="execution_uncertain")

    monkeypatch.setattr(service.approval_service, "create_standalone_contacts_proposal", fake_create)
    monkeypatch.setattr(service.approval_service, "get_proposal", fake_get)

    result = service.create_contacts_create_proposal(user_id=USER_ID, connection_id=CONNECTION_ID, context=ctx)
    assert result.replayed is True
    assert result.proposal.status == "execution_uncertain"
    assert called["create"] == 0
    assert called["get"] == 1
    assert "FOR UPDATE" in conn.statements[1][0]


def test_idempotency_service_returns_409_on_same_key_different_payload(monkeypatch):
    from app.services.contacts_idempotency_service import (
        ContactsIdempotencyConflictError,
        ContactsIdempotencyService,
    )

    ctx = build_contacts_create_idempotency_context(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        idempotency_key="same-key",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
    )
    conn = _FakeConnection(rows=[
        None,
        {
            "id": uuid.uuid4(),
            "proposal_id": uuid.uuid4(),
            "payload_fingerprint": "different-fingerprint",
            "normalization_version": ctx.normalization_version,
        },
    ])
    db = _FakeDB(conn)
    service = ContactsIdempotencyService(db)
    monkeypatch.setattr("app.services.contacts_idempotency_service.settings.CONTACTS_CREATE_ENABLED", True)

    with pytest.raises(ContactsIdempotencyConflictError):
        service.create_contacts_create_proposal(user_id=USER_ID, connection_id=CONNECTION_ID, context=ctx)


def test_idempotency_service_replays_existing_same_payload_even_after_replay_until(monkeypatch):
    """Safe retention: do not recycle key/create new proposal merely because replay window passed."""
    from app.services.contacts_idempotency_service import ContactsIdempotencyService

    existing_proposal_id = uuid.uuid4()
    ctx = build_contacts_create_idempotency_context(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        idempotency_key="same-key",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
    )
    conn = _FakeConnection(rows=[
        None,  # insert conflict
        {
            "id": uuid.uuid4(),
            "proposal_id": existing_proposal_id,
            "payload_fingerprint": ctx.payload_fingerprint,
            "normalization_version": ctx.normalization_version,
            "replay_until": "2000-01-01T00:00:00+00:00",
        },
    ])
    db = _FakeDB(conn)
    service = ContactsIdempotencyService(db)
    monkeypatch.setattr("app.services.contacts_idempotency_service.settings.CONTACTS_CREATE_ENABLED", True)
    monkeypatch.setattr(service.approval_service, "create_standalone_contacts_proposal", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")))
    monkeypatch.setattr(service.approval_service, "get_proposal", lambda user_id, proposal_id: _proposal(proposal_id=proposal_id, status="completed"))

    result = service.create_contacts_create_proposal(user_id=USER_ID, connection_id=CONNECTION_ID, context=ctx)
    assert result.replayed is True
    assert result.proposal.status == "completed"


def test_idempotency_service_does_not_recycle_executing_or_uncertain_after_replay_window(monkeypatch):
    from app.services.contacts_idempotency_service import ContactsIdempotencyService

    for status in ("executing", "execution_uncertain"):
        existing_proposal_id = uuid.uuid4()
        ctx = build_contacts_create_idempotency_context(
            user_id=USER_ID,
            connection_id=CONNECTION_ID,
            action_type="contacts.create_contact",
            idempotency_key=f"same-key-{status}",
            parameters={"display_name": "Ada", "email": "ada@example.com"},
        )
        conn = _FakeConnection(rows=[
            None,
            {
                "id": uuid.uuid4(),
                "proposal_id": existing_proposal_id,
                "payload_fingerprint": ctx.payload_fingerprint,
                "normalization_version": ctx.normalization_version,
                "replay_until": "2000-01-01T00:00:00+00:00",
            },
        ])
        db = _FakeDB(conn)
        service = ContactsIdempotencyService(db)
        monkeypatch.setattr("app.services.contacts_idempotency_service.settings.CONTACTS_CREATE_ENABLED", True)
        monkeypatch.setattr(service.approval_service, "create_standalone_contacts_proposal", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create")))
        monkeypatch.setattr(service.approval_service, "get_proposal", lambda user_id, proposal_id, s=status: _proposal(proposal_id=proposal_id, status=s))
        result = service.create_contacts_create_proposal(user_id=USER_ID, connection_id=CONNECTION_ID, context=ctx)
        assert result.replayed is True
        assert result.proposal.status == status


def test_idempotency_service_same_key_different_payload_conflicts_even_after_replay_until(monkeypatch):
    """Conservative retention: expired replay window must not allow key recycling."""
    from app.services.contacts_idempotency_service import ContactsIdempotencyConflictError, ContactsIdempotencyService

    ctx = build_contacts_create_idempotency_context(
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="contacts.create_contact",
        idempotency_key="same-key",
        parameters={"display_name": "Ada", "email": "ada@example.com"},
    )
    conn = _FakeConnection(rows=[
        None,
        {
            "id": uuid.uuid4(),
            "proposal_id": uuid.uuid4(),
            "payload_fingerprint": "different-fingerprint",
            "normalization_version": ctx.normalization_version,
            "replay_until": "2000-01-01T00:00:00+00:00",
        },
    ])
    db = _FakeDB(conn)
    service = ContactsIdempotencyService(db)
    monkeypatch.setattr("app.services.contacts_idempotency_service.settings.CONTACTS_CREATE_ENABLED", True)
    with pytest.raises(ContactsIdempotencyConflictError):
        service.create_contacts_create_proposal(user_id=USER_ID, connection_id=CONNECTION_ID, context=ctx)
