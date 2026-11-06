import asyncio
from contextlib import asynccontextmanager
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.services.gmail_service import GmailService


def test_archive_removes_inbox_label_and_preserves_all_mail_labels(monkeypatch):
    service = GmailService(db=None)
    user_id = uuid4()
    connection_id = uuid4()
    response = {
        "id": "message-123",
        "labelIds": ["UNREAD", "CATEGORY_PERSONAL"],
    }

    validate_connection = Mock()
    google_post = Mock()

    async def fake_google_post(**kwargs):
        google_post(**kwargs)
        return response

    monkeypatch.setattr(service, "_validate_connection", validate_connection)
    monkeypatch.setattr(service, "_google_post", fake_google_post)

    result = asyncio.run(
        service.archive_message(
            user_id=user_id,
            connection_id=connection_id,
            message_id="message-123",
        )
    )

    validate_connection.assert_called_once_with(
        user_id=user_id,
        connection_id=connection_id,
        required_scope=service.REQUIRED_MODIFY_SCOPE,
    )
    google_post.assert_called_once_with(
        user_id=user_id,
        connection_id=connection_id,
        endpoint="messages/message-123/modify",
        json_data={"removeLabelIds": ["INBOX"]},
    )
    assert result == {
        "id": "message-123",
        "label_ids": ["UNREAD", "CATEGORY_PERSONAL"],
        "status": "archived",
    }
    assert "INBOX" not in result["label_ids"]
    assert "TRASH" not in result["label_ids"]


def test_archived_message_remains_in_all_mail(monkeypatch):
    service = GmailService(db=None)
    user_id = uuid4()
    connection_id = uuid4()
    requests = []

    async def fake_google_get(**kwargs):
        requests.append(kwargs)
        if kwargs["endpoint"] == "messages":
            return {
                "messages": [{
                    "id": "message-123",
                    "threadId": "thread-123",
                }],
                "resultSizeEstimate": 1,
            }
        return {
            "id": "message-123",
            "labelIds": ["UNREAD", "CATEGORY_PERSONAL"],
            "payload": {},
        }

    @asynccontextmanager
    async def fake_api_session():
        yield object()

    monkeypatch.setattr(service, "_validate_connection", Mock())
    monkeypatch.setattr(service, "_google_get", fake_google_get)
    monkeypatch.setattr(service.api_client, "session", fake_api_session)

    result = asyncio.run(
        service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            folder="all",
        )
    )

    assert requests[0]["params"]["q"] == "in:anywhere -in:spam -in:trash"
    assert [item["id"] for item in result["messages"]] == ["message-123"]
    assert "INBOX" not in result["messages"][0]["labels"]


@pytest.mark.parametrize(
    "response",
    [
        {"id": "message-123", "labelIds": ["INBOX", "UNREAD"]},
        {"id": "message-123"},
        {},
    ],
)
def test_archive_rejects_unconfirmed_gmail_responses(monkeypatch, response):
    service = GmailService(db=None)

    async def fake_google_post(**_kwargs):
        return response

    monkeypatch.setattr(service, "_validate_connection", Mock())
    monkeypatch.setattr(service, "_google_post", fake_google_post)

    with pytest.raises(RuntimeError, match="Gmail API"):
        asyncio.run(
            service.archive_message(
                user_id=uuid4(),
                connection_id=uuid4(),
                message_id="message-123",
            )
        )
