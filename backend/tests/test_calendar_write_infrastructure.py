import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import (
    GoogleAPIMutationError,
)
from app.integrations.google.oauth_client import GoogleOAuthClient
from app.repositories.action_approval_repository import (
    ActionApprovalRepository,
    CalendarActionOperationRepository,
    _EXPIRE_PENDING,
    _INSERT_CALENDAR_OPERATION,
    _INSERT_PROPOSAL,
    _GET_PROPOSAL,
    _LOCK_PROPOSAL_IDEMPOTENCY,
    _LOCK_CALENDAR_APPROVAL_FOR_EXECUTION,
)
from app.db.models.calendar_action_operation import CalendarActionOperation
from app.services.gmail_action_executor import GmailActionExecutor
from app.services.calendar_service import CalendarService
from app.schemas.action_approval import ActionApprovalProposal
from app.schemas.calendar_approval import CalendarCreateEventParameters
from app.services.action_approval_service import (
    ActionApprovalService,
    ApprovalConflictError,
)
from app.services.calendar_action_executor import CalendarActionExecutor
from app.services.calendar_action_identity import (
    CalendarOperationConflictError,
    build_calendar_operation_identity,
    calendar_create_event_id,
    calendar_operation_id,
    calendar_payload_fingerprint,
    canonical_json_bytes,
    validate_idempotency_fingerprint_reuse,
    verify_calendar_proposal_integrity,
    verify_calendar_event_operation_marker,
)
from app.services.user_connection_service import UserConnectionService


USER_ID = uuid4()
CONNECTION_ID = uuid4()
PROPOSAL_ID = uuid4()
PARAMETERS = {
    "calendar_id": "shared@example.com",
    "title": "Design review",
    "starts_at": "2026-11-06T10:00:00+05:30",
    "ends_at": "2026-11-06T11:00:00+05:30",
    "time_zone": "Asia/Kolkata",
}


@pytest.mark.parametrize(
    ("service", "expected_scopes"),
    [
        (
            "calendar",
            {
                GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_LIST_READ_SCOPE,
                GoogleOAuthClient.CALENDAR_FREEBUSY_SCOPE,
                *GoogleOAuthClient.IDENTITY_SCOPES,
            },
        ),
        (
            "calendar_write",
            {GoogleOAuthClient.CALENDAR_EVENTS_WRITE_SCOPE},
        ),
    ],
)
def test_calendar_oauth_requests_only_service_scopes(service, expected_scopes):
    scopes = GoogleOAuthClient.scopes_for_service(service)

    assert set(scopes) == expected_scopes
    assert "https://www.googleapis.com/auth/gmail.readonly" not in scopes
    assert "https://www.googleapis.com/auth/calendar" not in scopes


def test_calendar_write_oauth_is_separate_incremental_consent():
    read_scopes = GoogleOAuthClient.scopes_for_service("calendar")
    write_scopes = GoogleOAuthClient.scopes_for_service("calendar_write")

    assert GoogleOAuthClient.CALENDAR_EVENTS_WRITE_SCOPE not in read_scopes
    assert GoogleOAuthClient.CALENDAR_EVENTS_WRITE_SCOPE in write_scopes
    assert write_scopes == [GoogleOAuthClient.CALENDAR_EVENTS_WRITE_SCOPE]


def test_calendar_list_oauth_is_separate_incremental_read_consent():
    read_scopes = GoogleOAuthClient.scopes_for_service("calendar")
    list_scopes = GoogleOAuthClient.scopes_for_service("calendar_list_read")

    assert GoogleOAuthClient.CALENDAR_LIST_READ_SCOPE in read_scopes
    assert GoogleOAuthClient.CALENDAR_EVENTS_READ_SCOPE in read_scopes
    assert GoogleOAuthClient.CALENDAR_EVENTS_WRITE_SCOPE not in read_scopes
    assert list_scopes == [GoogleOAuthClient.CALENDAR_LIST_READ_SCOPE]


def test_write_grant_is_reported_but_execution_remains_unavailable():
    connection = SimpleNamespace(
        id=CONNECTION_ID,
        provider="google",
        email="owner@example.com",
        status="connected",
        connected_at=None,
        created_at=None,
    )
    repository = Mock()
    repository.list_by_user.return_value = [connection]
    credentials = Mock()
    credentials.get_by_connection.return_value = SimpleNamespace(
        scopes=[
            UserConnectionService.CALENDAR_EVENTS_READ_SCOPE,
            UserConnectionService.CALENDAR_FREEBUSY_SCOPE,
            UserConnectionService.CALENDAR_EVENTS_WRITE_SCOPE,
        ],
    )
    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    capabilities = service.list_connections(USER_ID)[0]["capabilities"]

    assert capabilities["calendar_events_read"] is True
    assert capabilities["calendar_freebusy"] is True
    assert capabilities["calendar_write_permission_granted"] is True
    assert capabilities["calendar_write_execution_available"] is False
    assert capabilities["calendar_write_available"] is False


def test_read_only_grants_keep_calendar_reads_without_write_permission():
    connection = SimpleNamespace(
        id=CONNECTION_ID,
        provider="google",
        email="owner@example.com",
        status="connected",
        connected_at=None,
        created_at=None,
    )
    repository = Mock()
    repository.list_by_user.return_value = [connection]
    credentials = Mock()
    credentials.get_by_connection.return_value = SimpleNamespace(
        scopes=[
            UserConnectionService.CALENDAR_EVENTS_READ_SCOPE,
            UserConnectionService.CALENDAR_FREEBUSY_SCOPE,
        ],
    )
    service = UserConnectionService(db=None)
    service.repository = repository
    service.credential_repository = credentials

    capabilities = service.list_connections(USER_ID)[0]["capabilities"]

    assert capabilities["calendar_events_read"] is True
    assert capabilities["calendar_freebusy"] is True
    assert capabilities["calendar_write_permission_granted"] is False
    assert capabilities["calendar_write_execution_available"] is False


def test_calendar_mutation_execution_is_not_registered():
    assert GmailActionExecutor.SUPPORTED_ACTION_TYPES == frozenset({
        "gmail.send",
        "gmail.send_draft",
    })
    assert not hasattr(CalendarService, "create_event")
    assert not hasattr(CalendarService, "update_event")
    assert not hasattr(CalendarService, "delete_event")


def test_bulk_expiration_synchronizes_calendar_operation_state():
    sql = str(_EXPIRE_PENDING)

    assert "WITH expired_proposals AS" in sql
    assert "UPDATE public.pa_calendar_action_operations AS operation" in sql
    assert "SET execution_state = 'expired'" in sql
    assert "IN ('pending', 'approved')" in sql


def test_calendar_journal_retains_history_when_proposal_or_connection_is_deleted():
    foreign_key_targets = {
        next(iter(constraint.elements)).target_fullname
        for constraint in CalendarActionOperation.__table__.constraints
        if hasattr(constraint, "elements")
    }

    assert foreign_key_targets == {"auth.users.id"}


def test_calendar_execution_claim_locks_both_approval_and_journal_rows():
    sql = str(_LOCK_CALENDAR_APPROVAL_FOR_EXECUTION)

    assert "FOR UPDATE OF approval_message, operation" in sql
    assert "operation.user_id = conversation.user_id" in sql
    assert "operation.proposal_id = CAST(:proposal_id AS uuid)" in sql


def test_calendar_payload_fingerprint_is_canonical_and_owner_bound():
    reordered = dict(reversed(list(PARAMETERS.items())))
    fingerprint = calendar_payload_fingerprint(
        action_type="calendar.create_event",
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id=PARAMETERS["calendar_id"],
        parameters=PARAMETERS,
    )
    assert fingerprint == calendar_payload_fingerprint(
        action_type="calendar.create_event",
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        calendar_id=PARAMETERS["calendar_id"],
        parameters=reordered,
    )
    assert fingerprint != calendar_payload_fingerprint(
        action_type="calendar.create_event",
        user_id=uuid4(),
        connection_id=CONNECTION_ID,
        calendar_id=PARAMETERS["calendar_id"],
        parameters=PARAMETERS,
    )


@pytest.mark.parametrize(
    "value",
    [
        {"not_json": object()},
        {"tuple": (1, 2)},
        {"not_finite": float("nan")},
        {1: "not a string key"},
    ],
)
def test_canonical_json_rejects_non_json_values(value):
    with pytest.raises(ValueError):
        canonical_json_bytes(value)


def test_operation_and_google_event_ids_are_stable_and_valid():
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=PARAMETERS,
    )

    assert identity.operation_id == calendar_operation_id(
        PROPOSAL_ID,
        identity.payload_fingerprint,
    )
    assert identity.event_id == calendar_create_event_id(
        identity.operation_id,
        identity.payload_fingerprint,
    )
    assert len(identity.event_id) == 66
    assert set(identity.event_id) <= set("0123456789abcdefghijklmnopqrstuv")


def test_google_event_id_collision_requires_matching_private_operation_marker():
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=PARAMETERS,
    )
    event = {
        "id": identity.event_id,
        "extendedProperties": {
            "private": {
                "personal_ai_operation_id": str(identity.operation_id),
                "personal_ai_payload_fingerprint": identity.payload_fingerprint,
            },
        },
    }

    verify_calendar_event_operation_marker(event, identity)
    event["extendedProperties"]["private"]["personal_ai_operation_id"] = (
        str(uuid4())
    )
    with pytest.raises(CalendarOperationConflictError):
        verify_calendar_event_operation_marker(event, identity)


def test_proposal_integrity_check_detects_tampering_and_wrong_owner():
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=PARAMETERS,
    )
    proposal = SimpleNamespace(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=PARAMETERS,
        integrity_schema_version=identity.integrity_schema_version,
        payload_fingerprint=identity.payload_fingerprint,
        operation_id=identity.operation_id,
        calendar_event_id=identity.event_id,
    )

    assert verify_calendar_proposal_integrity(proposal, USER_ID) == identity
    proposal.parameters = {**PARAMETERS, "title": "Tampered"}
    with pytest.raises(CalendarOperationConflictError):
        verify_calendar_proposal_integrity(proposal, USER_ID)
    proposal.parameters = PARAMETERS
    with pytest.raises(CalendarOperationConflictError):
        verify_calendar_proposal_integrity(proposal, uuid4())


def test_approval_service_verifies_calendar_proposal_against_journal():
    now = datetime.now(timezone.utc)
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=PARAMETERS,
    )
    row = {
        "proposal_id": PROPOSAL_ID,
        "message_id": uuid4(),
        "source_message_id": uuid4(),
        "user_id": USER_ID,
        "conversation_id": uuid4(),
        "action_type": "calendar.create_event",
        "parameters": PARAMETERS,
        "connection_id": CONNECTION_ID,
        "created_at": now,
        "expires_at": now.replace(year=now.year + 1),
        "status": "pending",
        "integrity_schema_version": identity.integrity_schema_version,
        "payload_fingerprint": identity.payload_fingerprint,
        "operation_id": identity.operation_id,
        "calendar_event_id": identity.event_id,
    }
    service = ActionApprovalService.__new__(ActionApprovalService)
    service.repository = Mock()
    service.repository.get_proposal.return_value = row
    service.repository.recover_stale_gmail_send.return_value = None
    service.calendar_operation_repository = Mock()
    service.calendar_operation_repository.get_for_user.return_value = {
        "proposal_id": PROPOSAL_ID,
        "operation_id": identity.operation_id,
        "user_id": USER_ID,
        "connection_id": CONNECTION_ID,
        "action_type": identity.action_type,
        "calendar_id": identity.calendar_id,
        "event_id": identity.event_id,
        "payload_fingerprint": identity.payload_fingerprint,
        "execution_state": "pending",
    }

    proposal = service.get_proposal(USER_ID, PROPOSAL_ID)
    assert proposal.parameters == PARAMETERS
    service.calendar_operation_repository.get_for_user.assert_called_once_with(
        USER_ID,
        PROPOSAL_ID,
    )

    service.repository.get_proposal.return_value = {
        **row,
        "parameters": {**PARAMETERS, "title": "Tampered"},
    }
    with pytest.raises(
        ApprovalConflictError,
        match="integrity verification failed",
    ):
        service.get_proposal(USER_ID, PROPOSAL_ID)


def test_idempotency_key_matching_content_reuses_but_mismatch_conflicts():
    validate_idempotency_fingerprint_reuse("a" * 64, "a" * 64)
    with pytest.raises(CalendarOperationConflictError):
        validate_idempotency_fingerprint_reuse("a" * 64, "b" * 64)


class FakeHTTPClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def request(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _response(status_code, payload=None):
    return httpx.Response(
        status_code,
        json=payload if payload is not None else {},
        request=httpx.Request("PATCH", "https://www.googleapis.com/test"),
    )


def test_mutation_request_is_single_attempt_and_supports_if_match():
    fake_client = FakeHTTPClient(response=_response(503))
    client = GoogleAPIClient()

    with pytest.raises(GoogleAPIMutationError) as raised:
        asyncio.run(
            client._send_mutation_request(
                fake_client,
                "PATCH",
                "https://www.googleapis.com/calendar/v3/calendars/primary/events/event",
                {"If-Match": '"etag-1"'},
                None,
                {"summary": "updated"},
            )
        )

    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["headers"]["If-Match"] == '"etag-1"'
    assert raised.value.outcome_unknown is True


@pytest.mark.parametrize(
    ("error", "outcome_unknown"),
    [
        (
            httpx.ConnectError("connection refused"),
            False,
        ),
        (
            httpx.ReadTimeout(
                "read timeout",
                request=httpx.Request(
                    "DELETE",
                    "https://www.googleapis.com/calendar/v3/test",
                ),
            ),
            True,
        ),
    ],
)
def test_mutation_transport_failure_outcome(error, outcome_unknown):
    fake_client = FakeHTTPClient(error=error)
    with pytest.raises(GoogleAPIMutationError) as raised:
        asyncio.run(
            GoogleAPIClient()._send_mutation_request(
                fake_client,
                "DELETE",
                "https://www.googleapis.com/calendar/v3/test",
                {},
                None,
                None,
            )
        )
    assert len(fake_client.calls) == 1
    assert raised.value.outcome_unknown is outcome_unknown


def test_remote_protocol_failure_is_uncertain():
    fake_client = FakeHTTPClient(
        error=httpx.RemoteProtocolError("connection closed unexpectedly"),
    )
    with pytest.raises(GoogleAPIMutationError) as raised:
        asyncio.run(
            GoogleAPIClient()._send_mutation_request(
                fake_client,
                "DELETE",
                "https://www.googleapis.com/calendar/v3/test",
                {"If-Match": '"etag-1"'},
                None,
                None,
            )
        )
    assert raised.value.outcome_unknown is True


def test_update_and_delete_mutations_require_if_match():
    client = GoogleAPIClient()
    for method in ("PUT", "PATCH", "DELETE"):
        with pytest.raises(ValueError, match="If-Match"):
            asyncio.run(
                client.request_mutation(
                    db=Mock(),
                    user_id=str(USER_ID),
                    connection_id=str(CONNECTION_ID),
                    method=method,
                    url="https://www.googleapis.com/calendar/v3/test",
                )
            )


def test_mutation_request_api_passes_if_match_without_refresh_retry(monkeypatch):
    token_service = Mock()
    token_service.get_valid_access_token = AsyncMock(return_value="token")
    monkeypatch.setattr(
        "app.integrations.google.api_client.OAuthCredentialService",
        lambda _db: token_service,
    )
    fake_client = FakeHTTPClient(response=_response(204))
    db = Mock()

    result = asyncio.run(
        GoogleAPIClient().request_mutation(
            db=db,
            user_id=str(USER_ID),
            connection_id=str(CONNECTION_ID),
            method="DELETE",
            url="https://www.googleapis.com/calendar/v3/calendars/primary/events/event",
            headers={"If-Match": '"etag-2"'},
            http_client=fake_client,
        )
    )

    assert result == {}
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["headers"]["If-Match"] == '"etag-2"'
    assert fake_client.calls[0]["headers"]["Authorization"] == "Bearer token"
    assert db.commit.call_count == 2
    token_service.get_valid_access_token.assert_awaited_once()


def test_existing_google_request_retry_behavior_is_unchanged(monkeypatch):
    responses = [_response(503), _response(503), _response(503), _response(200)]
    fake_client = FakeHTTPClient()

    async def response_sequence(**kwargs):
        fake_client.calls.append(kwargs)
        return responses.pop(0)

    fake_client.request = response_sequence
    monkeypatch.setattr(
        "app.integrations.google.api_client.asyncio.sleep",
        AsyncMock(),
    )

    result = asyncio.run(
        GoogleAPIClient()._send_request(
            fake_client,
            "PUT",
            "https://www.googleapis.com/calendar/v3/test",
            {},
            None,
            {},
        )
    )

    assert result == {}
    assert len(fake_client.calls) == 4


def test_uncertain_operation_cannot_transition_without_reconciliation():
    repository = CalendarActionOperationRepository(database_engine=Mock())
    with pytest.raises(ValueError, match="positive reconciliation"):
        repository.transition(
            user_id=USER_ID,
            proposal_id=PROPOSAL_ID,
            expected_state="execution_uncertain",
            new_state="completed",
        )
    with pytest.raises(ValueError, match="positive reconciliation"):
        repository.transition(
            user_id=USER_ID,
            proposal_id=PROPOSAL_ID,
            expected_state="execution_uncertain",
            new_state="failed",
            reconciliation_status="not_found",
        )


def test_operation_recovery_query_is_scoped_to_user_and_proposal():
    row = {
        "proposal_id": PROPOSAL_ID,
        "user_id": USER_ID,
        "execution_state": "execution_uncertain",
        "reconciliation_status": "pending",
        "attempt_count": 1,
        "provider_result_classification": "outcome_unknown",
    }

    class Result:
        def mappings(self):
            return self

        def first(self):
            return row

    class Connection:
        parameters = None

        def execute(self, _statement, parameters):
            self.parameters = parameters
            return Result()

    connection = Connection()

    class Engine:
        def connect(self):
            return self

        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    recovered = CalendarActionOperationRepository(
        database_engine=Engine(),
    ).get_for_user(USER_ID, PROPOSAL_ID)

    assert recovered["execution_state"] == "execution_uncertain"
    assert connection.parameters == {
        "user_id": str(USER_ID),
        "proposal_id": str(PROPOSAL_ID),
    }


def test_calendar_operation_is_inserted_with_proposal_in_one_transaction():
    now = datetime.now(timezone.utc).isoformat()
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=PARAMETERS,
    )
    approval = {
        "proposal_id": str(PROPOSAL_ID),
        "source_message_id": str(uuid4()),
        "user_id": str(USER_ID),
        "conversation_id": str(uuid4()),
        "connection_id": str(CONNECTION_ID),
        "action_type": "calendar.create_event",
        "parameters": PARAMETERS,
        "created_at": now,
        "expires_at": now,
        "status": "pending",
        "integrity_schema_version": 1,
        "payload_fingerprint": identity.payload_fingerprint,
        "operation_id": str(identity.operation_id),
        "calendar_event_id": identity.event_id,
    }
    proposal_row = {
        "message_id": str(PROPOSAL_ID),
        "metadata": {"action_approval": approval},
    }

    class Result:
        def __init__(self, value=None):
            self.value = value

        def mappings(self):
            return self

        def first(self):
            return self.value

    class Connection:
        def __init__(self):
            self.statements = []

        def execute(self, statement, params):
            self.statements.append((statement, params))
            if statement is _LOCK_PROPOSAL_IDEMPOTENCY:
                return Result()
            if statement is _GET_PROPOSAL:
                return Result()
            if statement is _INSERT_PROPOSAL:
                return Result(proposal_row)
            assert statement is _INSERT_CALENDAR_OPERATION
            return Result()

    connection = Connection()

    class Transaction:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    class Engine:
        def begin(self):
            return Transaction()

    operation = {
        "proposal_id": str(PROPOSAL_ID),
        "operation_id": str(identity.operation_id),
        "user_id": str(USER_ID),
        "connection_id": str(CONNECTION_ID),
        "idempotency_key": None,
        "payload_fingerprint": identity.payload_fingerprint,
        "action_type": "calendar.create_event",
        "calendar_id": PARAMETERS["calendar_id"],
        "event_id": identity.event_id,
        "integrity_schema_version": 1,
    }
    result = ActionApprovalRepository(
        database_engine=Engine(),
    ).create_proposal(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        conversation_id=uuid4(),
        source_message_id=uuid4(),
        action_type="calendar.create_event",
        parameters=PARAMETERS,
        connection_id=CONNECTION_ID,
        account_email="owner@example.com",
        expiration_minutes=15,
        calendar_operation=operation,
    )

    assert [statement for statement, _params in connection.statements] == [
        _LOCK_PROPOSAL_IDEMPOTENCY,
        _GET_PROPOSAL,
        _INSERT_PROPOSAL,
        _INSERT_CALENDAR_OPERATION,
    ]
    assert result["payload_fingerprint"] == identity.payload_fingerprint
    assert result["operation_id"] == identity.operation_id
    assert connection.statements[3][1]["event_id"] == identity.event_id


def test_calendar_proposal_retry_reuses_existing_pending_record():
    now = datetime.now(timezone.utc).isoformat()
    conversation_id = uuid4()
    source_message_id = uuid4()
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=PARAMETERS,
    )
    approval = {
        "proposal_id": str(PROPOSAL_ID),
        "source_message_id": str(source_message_id),
        "user_id": str(USER_ID),
        "conversation_id": str(conversation_id),
        "connection_id": str(CONNECTION_ID),
        "action_type": "calendar.create_event",
        "parameters": PARAMETERS,
        "created_at": now,
        "expires_at": now,
        "status": "pending",
        "integrity_schema_version": 1,
        "payload_fingerprint": identity.payload_fingerprint,
        "operation_id": str(identity.operation_id),
        "calendar_event_id": identity.event_id,
    }

    class Result:
        def __init__(self, value=None):
            self.value = value

        def mappings(self):
            return self

        def first(self):
            return self.value

    class Connection:
        def __init__(self):
            self.statements = []
            self.proposal_row = None

        def execute(self, statement, params):
            self.statements.append((statement, params))
            if statement is _LOCK_PROPOSAL_IDEMPOTENCY:
                return Result()
            if statement is _GET_PROPOSAL:
                return Result(self.proposal_row)
            if statement is _INSERT_PROPOSAL:
                self.proposal_row = {
                    "message_id": str(PROPOSAL_ID),
                    "metadata": {"action_approval": approval},
                }
                return Result(self.proposal_row)
            assert statement is _INSERT_CALENDAR_OPERATION
            return Result()

    connection = Connection()

    class Transaction:
        def __enter__(self):
            return connection

        def __exit__(self, *_args):
            return False

    class Engine:
        def begin(self):
            return Transaction()

    repository = ActionApprovalRepository(database_engine=Engine())
    arguments = {
        "proposal_id": PROPOSAL_ID,
        "user_id": USER_ID,
        "conversation_id": conversation_id,
        "source_message_id": source_message_id,
        "action_type": "calendar.create_event",
        "parameters": PARAMETERS,
        "connection_id": CONNECTION_ID,
        "account_email": "owner@example.com",
        "expiration_minutes": 15,
        "calendar_operation": {
            "proposal_id": str(PROPOSAL_ID),
            "operation_id": str(identity.operation_id),
            "user_id": str(USER_ID),
            "connection_id": str(CONNECTION_ID),
            "idempotency_key": None,
            "payload_fingerprint": identity.payload_fingerprint,
            "action_type": "calendar.create_event",
            "calendar_id": PARAMETERS["calendar_id"],
            "event_id": identity.event_id,
            "integrity_schema_version": 1,
        },
    }

    first = repository.create_proposal(**arguments)
    second = repository.create_proposal(**arguments)

    assert first["proposal_id"] == second["proposal_id"] == PROPOSAL_ID
    assert first["status"] == second["status"] == "pending"
    assert sum(
        statement is _INSERT_PROPOSAL
        for statement, _params in connection.statements
    ) == 1
    assert sum(
        statement is _INSERT_CALENDAR_OPERATION
        for statement, _params in connection.statements
    ) == 1


def _create_event_parameters(**overrides):
    values = {
        "calendar_id": "primary",
        "summary": "Design review",
        "description": None,
        "start": "2026-06-10T10:00:00-07:00",
        "end": "2026-06-10T11:00:00-07:00",
        "time_zone": "America/Los_Angeles",
        "location": None,
        "attendees": [],
        "all_day": False,
    }
    return {**values, **overrides}


def test_calendar_create_schema_accepts_both_explicit_dst_folds():
    parameters = CalendarCreateEventParameters.model_validate(
        _create_event_parameters(
            start="2026-11-01T01:30:00-04:00",
            end="2026-11-01T01:30:00-05:00",
            time_zone="America/New_York",
        )
    )

    assert parameters.start.endswith("-04:00")
    assert parameters.end.endswith("-05:00")


def test_calendar_create_schema_rejects_end_before_start():
    with pytest.raises(ValueError, match="end must be later than start"):
        CalendarCreateEventParameters.model_validate(
            _create_event_parameters(
                start="2026-06-10T11:00:00-07:00",
                end="2026-06-10T10:00:00-07:00",
            )
        )


@pytest.mark.parametrize(
    "values",
    [
        _create_event_parameters(
            start="2026-03-08T02:30:00-05:00",
            end="2026-03-08T03:30:00-04:00",
            time_zone="America/New_York",
        ),
        _create_event_parameters(
            start="2026-06-10T10:00:00",
            end="2026-06-10T11:00:00",
        ),
        _create_event_parameters(time_zone="PST"),
        _create_event_parameters(
            start="2026-06-10T10:00:00-08:00",
            time_zone="America/Los_Angeles",
        ),
        {
            **_create_event_parameters(),
            "unexpected": "field",
        },
    ],
)
def test_calendar_create_schema_rejects_ambiguous_or_malformed_inputs(values):
    with pytest.raises(ValueError):
        CalendarCreateEventParameters.model_validate(values)


def test_calendar_create_schema_enforces_all_day_exclusive_end():
    parameters = CalendarCreateEventParameters.model_validate(
        _create_event_parameters(
            start="2026-06-10",
            end="2026-06-11",
            time_zone=None,
            all_day=True,
        )
    )
    assert parameters.end == "2026-06-11"

    with pytest.raises(ValueError, match="exclusive start date"):
        CalendarCreateEventParameters.model_validate(
            _create_event_parameters(
                start="2026-06-10",
                end="2026-06-10",
                time_zone=None,
                all_day=True,
            )
        )


def test_calendar_create_schema_normalizes_and_limits_attendees():
    parameters = CalendarCreateEventParameters.model_validate(
        _create_event_parameters(
            attendees=[
                "person@example.com",
                "PERSON@example.com",
            ],
        )
    )
    assert parameters.attendees == ["person@example.com"]

    with pytest.raises(ValueError, match="invalid email"):
        CalendarCreateEventParameters.model_validate(
            _create_event_parameters(attendees=["not-an-email"])
        )
    with pytest.raises(ValueError):
        CalendarCreateEventParameters.model_validate(
            _create_event_parameters(
                attendees=[f"person{index}@example.com" for index in range(51)]
            )
        )


def test_calendar_create_event_body_has_stable_id_and_private_markers():
    parameters = CalendarCreateEventParameters.model_validate(
        _create_event_parameters(attendees=["guest@example.com"])
        | {
            "reminders": [
                {"method": "popup", "minutes": 10},
                {"method": "email", "minutes": 60},
            ],
        }
    )
    identity = build_calendar_operation_identity(
        proposal_id=PROPOSAL_ID,
        user_id=USER_ID,
        connection_id=CONNECTION_ID,
        action_type="calendar.create_event",
        parameters=parameters.model_dump(mode="json"),
    )

    body = CalendarActionExecutor._event_body(identity, parameters)

    assert body["id"] == identity.event_id
    assert body["start"] == {
        "dateTime": parameters.start,
        "timeZone": parameters.time_zone,
    }
    assert body["extendedProperties"]["private"] == {
        "personal_ai_operation_id": str(identity.operation_id),
        "personal_ai_payload_fingerprint": identity.payload_fingerprint,
    }
    assert body["attendees"] == [{"email": "guest@example.com"}]
    assert body["reminders"] == {
        "useDefault": False,
        "overrides": [
            {"method": "popup", "minutes": 10},
            {"method": "email", "minutes": 60},
        ],
    }


def test_calendar_create_rejects_invalid_reminder_values():
    with pytest.raises(ValueError):
        CalendarCreateEventParameters.model_validate(
            _create_event_parameters(
                reminders=[{"method": "sms", "minutes": 10}],
            )
        )
    with pytest.raises(ValueError):
        CalendarCreateEventParameters.model_validate(
            _create_event_parameters(
                reminders=[{"method": "popup", "minutes": True}],
            )
        )
