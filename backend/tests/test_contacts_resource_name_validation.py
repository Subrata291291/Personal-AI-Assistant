from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.action_approval import ApprovalProposalCreate


CONNECTION_ID = uuid4()
MAX_RESOURCE_NAME_LENGTH = 512
VALID_IDENTIFIER = "c123456789"


def _payload(action_type, parameters):
    return ApprovalProposalCreate.model_validate({
        "conversation_id": uuid4(),
        "source_message_id": uuid4(),
        "connection_id": CONNECTION_ID,
        "action_type": action_type,
        "parameters": parameters,
    })


@pytest.mark.parametrize(
    "resource_name",
    [
        f"people/{VALID_IDENTIFIER}",
        "people/me",
        "people/ABC_def-123",
        # 7 characters for "people/" plus 505 characters == 512 characters.
        "people/" + "a" * (MAX_RESOURCE_NAME_LENGTH - len("people/")),
    ],
)
def test_update_contact_accepts_people_resource_names(resource_name):
    payload = _payload(
        "contacts.update_contact",
        {"contact_id": resource_name, "display_name": "Ada", "etag": "etag-1"},
    )

    assert payload.parameters["contact_id"] == resource_name


@pytest.mark.parametrize(
    "resource_name",
    [
        f"people/{VALID_IDENTIFIER}",
        "people/me",
        "people/ABC_def-123",
        "people/" + "a" * (MAX_RESOURCE_NAME_LENGTH - len("people/")),
    ],
)
def test_delete_contact_accepts_people_resource_names(resource_name):
    payload = _payload(
        "contacts.delete_contact",
        {"contact_id": resource_name},
    )

    assert payload.parameters["contact_id"] == resource_name


@pytest.mark.parametrize(
    "resource_name",
    [
        "people/",
        VALID_IDENTIFIER,
        f"person/{VALID_IDENTIFIER}",
        "people/a/b",
        "people//",
        f" people/{VALID_IDENTIFIER}",
        f"people/{VALID_IDENTIFIER} ",
        " people/c123456789 ",
        "People/c123456789",
        "people/c123 456",
        "people/c123@456",
        "people/c123:456",
        "people/c123#456",
        "people/c123\t456",
        "people/c123\n456",
        "people/" + "a" * (MAX_RESOURCE_NAME_LENGTH - len("people/") + 1),
    ],
)
def test_update_contact_rejects_invalid_resource_names(resource_name):
    with pytest.raises(ValidationError, match="contact_id is invalid"):
        _payload(
            "contacts.update_contact",
            {"contact_id": resource_name, "display_name": "Ada", "etag": "etag-1"},
        )


@pytest.mark.parametrize(
    "resource_name",
    [
        "people/",
        VALID_IDENTIFIER,
        f"person/{VALID_IDENTIFIER}",
        "people/a/b",
        f" people/{VALID_IDENTIFIER}",
        f"people/{VALID_IDENTIFIER} ",
        "people/" + "a" * (MAX_RESOURCE_NAME_LENGTH - len("people/") + 1),
    ],
)
def test_delete_contact_rejects_invalid_resource_names(resource_name):
    with pytest.raises(ValidationError, match="contact_id is invalid"):
        _payload(
            "contacts.delete_contact",
            {"contact_id": resource_name},
        )


def test_invalid_resource_name_is_rejected_before_any_google_call():
    google_people_api = Mock()

    with pytest.raises(ValidationError, match="contact_id is invalid"):
        _payload(
            "contacts.update_contact",
            {"contact_id": "person/c123456789", "display_name": "Ada", "etag": "etag-1"},
        )

    google_people_api.assert_not_called()


def test_unrelated_resource_id_validation_is_unchanged():
    # The generic resource identifier pattern still rejects a slash.
    with pytest.raises(ValidationError, match="message_id is invalid"):
        _payload("gmail.archive", {"message_id": "people/c123456789"})

    payload = _payload("gmail.archive", {"message_id": "message-123_ok:1"})
    assert payload.parameters["message_id"] == "message-123_ok:1"
