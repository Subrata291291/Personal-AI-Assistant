import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

from app.services.gmail_service import GmailService


def test_message_summary_preserves_internal_date_separately_from_date_header(
    monkeypatch,
):
    service = GmailService(db=None)
    user_id = uuid4()
    connection_id = uuid4()
    requests = []
    gmail_internal_date = "1793815200000"
    sender_date = "Mon, 01 Jan 2001 00:00:00 +0000"

    async def fake_google_get(**kwargs):
        requests.append(kwargs)
        if kwargs["endpoint"] == "messages":
            return {
                "messages": [{
                    "id": "message-1",
                    "threadId": "thread-1",
                }],
                "resultSizeEstimate": 1,
            }
        return {
            "id": "message-1",
            "threadId": "thread-1",
            "snippet": "Preview",
            "internalDate": gmail_internal_date,
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "From", "value": "Sender <sender@example.com>"},
                    {"name": "Date", "value": sender_date},
                ],
            },
        }

    @asynccontextmanager
    async def fake_api_session():
        yield object()

    monkeypatch.setattr(service, "_validate_connection", lambda *_args: None)
    monkeypatch.setattr(service, "_google_get", fake_google_get)
    monkeypatch.setattr(service.api_client, "session", fake_api_session)

    result = asyncio.run(
        service.list_messages(
            user_id=user_id,
            connection_id=connection_id,
            folder="inbox",
        )
    )

    assert result["messages"] == [{
        "id": "message-1",
        "thread_id": "thread-1",
        "snippet": "Preview",
        "sender": "Sender <sender@example.com>",
        "recipient": None,
        "subject": None,
        "internal_date": gmail_internal_date,
        "date": sender_date,
        "labels": ["INBOX"],
    }]
    assert requests[1]["params"]["format"] == "metadata"
    assert "Date" in requests[1]["params"]["metadataHeaders"]
