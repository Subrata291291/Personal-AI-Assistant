from unittest.mock import Mock
from uuid import uuid4

from app.repositories.memory_repository import MemoryRepository
from app.repositories.memory_review_repository import MemoryReviewRepository
from app.services.conversation_service import ConversationService
from app.services.user_connection_service import UserConnectionService


def test_fresh_user_has_empty_google_connections_and_calendar_capabilities():
    user_id = uuid4()
    service = UserConnectionService(db=Mock())
    service.repository = Mock()
    service.repository.list_by_user.return_value = []

    connections = service.list_connections(user_id=user_id)
    capabilities = service.aggregate_calendar_capabilities(connections)

    assert connections == []
    assert capabilities == {
        "connected": False,
        "events_read": False,
        "freebusy": False,
        "api_available": False,
        "api_complete": False,
        "chat_available": False,
        "google_connection_available": False,
        "permissions_need_consent": False,
        "write_permission_granted": False,
        "write_execution_available": False,
        "write_available": False,
    }


def test_fresh_user_has_empty_memories():
    session = Mock()
    session.scalars.return_value.all.return_value = []

    assert MemoryRepository(session).list_memories(
        user_id=uuid4(),
        memory_type="long_term",
        limit=100,
        offset=0,
    ) == []


def test_fresh_user_has_empty_memory_reviews():
    session = Mock()
    session.scalars.return_value.all.return_value = []

    assert MemoryReviewRepository(session).list_reviews(
        user_id=uuid4(),
        status="pending",
        limit=100,
        offset=0,
    ) == []


def test_fresh_user_has_empty_conversations_scoped_to_their_identity():
    user_id = uuid4()
    client = Mock()
    query = client.table.return_value.select.return_value
    query.eq.return_value.order.return_value.execute.return_value.data = []

    conversations = ConversationService(client).list_conversations(user_id)

    assert conversations == []
    query.eq.assert_called_once_with("user_id", str(user_id))
