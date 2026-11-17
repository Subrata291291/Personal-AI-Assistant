import logging

from app.core.diagnostics import lifecycle_event
from app.core.request_metrics import RequestMetrics, current_request_metrics


def test_lifecycle_event_includes_required_fields_and_redacts_sensitive(caplog):
    token = current_request_metrics.set(RequestMetrics(request_id="req-123"))
    try:
        with caplog.at_level(logging.INFO, logger="app.diagnostics"):
            lifecycle_event(
                "chat.graph.completed",
                user_id="user-uuid-1",
                conversation_id="conv-uuid-1",
                assistant_route="gmail",
                intent="summarize_today",
                provider_call_occurred=True,
                llm_call_occurred=True,
                duration_ms=12.34,
                # sensitive fields that must not appear as raw values
                email="alice@example.com",
                phone="+15551234567",
                message="show me my emails",
                access_token="secret-token",
                raw_payload={"x": "y"},
            )
    finally:
        current_request_metrics.reset(token)

    records = [r for r in caplog.records if "diag_event=chat.graph.completed" in r.getMessage()]
    assert records, "expected lifecycle diagnostic event"
    msg = records[-1].getMessage()
    # required/stable fields
    assert "diag_event=chat.graph.completed" in msg
    assert "event=chat.graph.completed" in msg
    assert "request_id=req-123" in msg
    assert "user_ref=" in msg
    assert "conversation_ref=" in msg
    assert "assistant_route=gmail" in msg
    assert "intent=summarize_today" in msg
    assert "provider_call_occurred=True" in msg
    assert "llm_call_occurred=True" in msg
    # redaction/privacy: no raw IDs or sensitive values
    assert "user-uuid-1" not in msg
    assert "conv-uuid-1" not in msg
    assert "alice@example.com" not in msg
    assert "+15551234567" not in msg
    assert "show me my emails" not in msg
    assert "secret-token" not in msg
    assert "raw_payload" not in msg or "{'x': 'y'}" not in msg
