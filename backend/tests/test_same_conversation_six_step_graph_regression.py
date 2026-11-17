import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.llm.schemas import LLMResponse
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import gmail_agent_nodes
from app.workflows.chat_graph import ASSISTANT_ROUTE_NODES


USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CAL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
CONTACTS_CONNECTION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
GMAIL_CONNECTION_ID = UUID("bbbbbbbb-cccc-dddd-eeee-ffffffffffff")


class FakeRequestRepository:
    def __init__(self):
        self.request_ids = []

    def create_request(self, **_kwargs):
        rid = uuid4()
        self.request_ids.append(rid)
        return {"id": rid, "status": "processing"}, True

    def claim_request(self, **_kwargs):
        return {"id": self.request_ids[-1]}

    def attach_user_message(self, **_kwargs):
        return {"attached": True}

    def mark_execution_started(self, **_kwargs):
        return {"started": True}

    def update_status(self, **_kwargs):
        return {"updated": True}


class FakeMessageService:
    def __init__(self):
        self.messages = []

    def verify_conversation_ownership(self, **_kwargs):
        return True

    def get_request_message(self, *, role, chat_request_id=None, **_kwargs):
        for message in reversed(self.messages):
            if message["role"] != role:
                continue
            if chat_request_id is not None and message.get("chat_request_id") != chat_request_id:
                continue
            return message
        return None

    def create_request_message(self, *, conversation_id, role, content, metadata=None, chat_request_id=None, **_kwargs):
        message = {
            "id": uuid4(),
            "conversation_id": conversation_id,
            "chat_request_id": chat_request_id,
            "role": role,
            "content": content,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc),
        }
        self.messages.append(message)
        return message

    def get_message(self, *, message_id, **_kwargs):
        return next((m for m in self.messages if m["id"] == message_id), None)

    def get_conversation_messages(self, **_kwargs):
        return list(self.messages)

    def update_request_message_content(self, *, message_id, content, metadata=None, **_kwargs):
        for m in self.messages:
            if m["id"] == message_id:
                m["content"] = content
                if metadata is not None:
                    m["metadata"] = metadata
                return m
        return None


class FakeUserConnectionService:
    db = object()

    def get_granted_google_scopes(self, **_kwargs):
        return set()

    def get_default_google_connection_for_scopes(self, **_kwargs):
        return None

    def get_user_calendar_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {
                "write_permission_granted": True,
                "calendar_write_execution_available": False,
                "calendar_update_execution_available": False,
                "calendar_create_execution_available": False,
                "calendar_delete_execution_available": True,
                "calendar_update_feature_enabled": False,
            },
            "event_read_connection_id": str(CAL_CONNECTION_ID),
            "freebusy_connection_id": None,
            "write_connection_id": str(CAL_CONNECTION_ID),
            "default_connection_invalid": False,
            "default_connection_selected": True,
            "selected_connection_email": "user@example.com",
            "event_selection_required": False,
            "freebusy_selection_required": False,
            "selected_event_scope_missing": False,
            "selected_freebusy_scope_missing": False,
            "calendar_write_selection_required": False,
            "calendar_selected_write_scope_missing": False,
        }

    def get_user_contacts_access(self, **_kwargs):
        return {
            "google_connection_available": True,
            "capabilities": {},
            "read_connection_id": str(CONTACTS_CONNECTION_ID),
            "write_connection_id": None,
            "selected_connection_email": "user@example.com",
            "default_connection_invalid": False,
            "default_connection_selected": True,
            "selection_required": False,
            "candidate_emails": ["user@example.com"],
            "read_scope_missing": False,
            "write_scope_missing": False,
        }

    def get_user_drive_access(self, **_kwargs):
        return {
            "google_connection_available": False,
            "capabilities": {},
            "connection_id": None,
            "selected_connection_email": None,
            "default_connection_invalid": False,
            "default_connection_selected": False,
            "selection_required": False,
            "candidate_emails": [],
            "read_scope_missing": False,
            "enabled": False,
        }


def _make_service():
    message_service = FakeMessageService()
    request_repository = FakeRequestRepository()
    user_connection_service = FakeUserConnectionService()
    service = ChatService(
        message_service=message_service,
        user_settings_service=SimpleNamespace(get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))),
        memory_pipeline=SimpleNamespace(process=Mock(return_value=None)),
        request_repository=request_repository,
        user_connection_service=user_connection_service,
    )
    return service, message_service, request_repository


def _norm_event(eid, summary, start_dt, end_dt, tz_name="Asia/Kolkata"):
    tz = ZoneInfo(tz_name)
    return {
        "id": eid,
        "summary": summary,
        "description": None,
        "location": None,
        "status": "confirmed",
        "etag": f"etag-{eid}",
        "html_link": f"https://calendar.google.com/event?eid={eid}",
        "start": {"date": None, "date_time": start_dt.astimezone(timezone.utc), "time_zone": tz_name},
        "end": {"date": None, "date_time": end_dt.astimezone(timezone.utc), "time_zone": tz_name},
        "attendees": [],
        "recurrence": None,
        "recurring_event_id": None,
        "original_start_time": None,
        "updated": None,
    }


def _send(service, content):
    token = current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
    try:
        return service.send_message(
            user_id=USER_ID,
            conversation_id=CONVERSATION_ID,
            content=content,
            idempotency_key=uuid4(),
            user_timezone="Asia/Kolkata",
            gmail_connection_id=GMAIL_CONNECTION_ID,
            default_calendar_connection_id=str(CAL_CONNECTION_ID),
            default_contacts_connection_id=str(CONTACTS_CONNECTION_ID),
            gmail_enabled=True,
            calendar_enabled=True,
            contacts_enabled=True,
            drive_enabled=False,
        )
    finally:
        current_request_metrics.reset(token)


def test_same_conversation_exact_six_step_graph_regression(monkeypatch):
    service, message_service, _ = _make_service()

    # Disable planner LLM ambiguity and general LLM chatter; keep graph/service orchestration real.
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    from app.workflows import chat_nodes

    monkeypatch.setattr(
        chat_nodes.llm_router,
        "generate",
        Mock(
            return_value=LLMResponse(
                content="OK",
                provider="test",
                model="test",
                finish_reason="stop",
                latency_ms=0,
                output_limited=False,
            )
        ),
    )
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)

    tz = ZoneInfo("Asia/Kolkata")
    target_event = _norm_event(
        "evt-10nov-target",
        "Personal AI Assistant - Calendar Test",
        datetime(2026, 11, 10, 11, 0, tzinfo=tz),
        datetime(2026, 11, 10, 12, 0, tzinfo=tz),
    )
    other_event_1 = _norm_event(
        "evt-10nov-1",
        "Planning",
        datetime(2026, 11, 10, 9, 0, tzinfo=tz),
        datetime(2026, 11, 10, 10, 0, tzinfo=tz),
    )
    other_event_2 = _norm_event(
        "evt-10nov-3",
        "Review",
        datetime(2026, 11, 10, 14, 0, tzinfo=tz),
        datetime(2026, 11, 10, 15, 0, tzinfo=tz),
    )
    tenth_nov_events = [other_event_1, target_event, other_event_2]
    upcoming_events = [
        _norm_event(
            "evt-upcoming-1",
            "Daily Sync",
            datetime(2026, 11, 18, 10, 0, tzinfo=tz),
            datetime(2026, 11, 18, 10, 30, tzinfo=tz),
        )
    ]

    calendar_calls = {"list_events": [], "get_event": [], "validate_event_write_access": []}

    async def list_events(**kw):
        calendar_calls["list_events"].append(kw)
        tmin = kw.get("time_min")
        tmax = kw.get("time_max")
        # 10 Nov discovery window (Asia/Kolkata day)
        if tmin and tmax:
            try:
                tmin_dt = tmin if isinstance(tmin, datetime) else datetime.fromisoformat(str(tmin).replace("Z", "+00:00"))
                tmax_dt = tmax if isinstance(tmax, datetime) else datetime.fromisoformat(str(tmax).replace("Z", "+00:00"))
                if (
                    tmin_dt.astimezone(tz).date().isoformat() == "2026-11-10"
                    and tmax_dt.astimezone(tz).date().isoformat() == "2026-11-11"
                ):
                    return {"events": list(tenth_nov_events), "next_page_token": None, "calendar_id": kw.get("calendar_id", "primary")}
            except Exception:
                pass
        # Upcoming read: default to upcoming events for non-10-Nov queries
        return {"events": list(upcoming_events), "next_page_token": None, "calendar_id": kw.get("calendar_id", "primary")}

    async def get_event(**kw):
        calendar_calls["get_event"].append(kw)
        eid = kw.get("event_id")
        for ev in tenth_nov_events + upcoming_events:
            if ev["id"] == eid:
                return ev
        return None

    async def validate_event_write_access(**kw):
        calendar_calls["validate_event_write_access"].append(kw)
        eid = kw.get("event_id")
        for ev in tenth_nov_events:
            if ev["id"] == eid:
                return ev
        return target_event

    cal_service = SimpleNamespace(
        list_events=AsyncMock(side_effect=list_events),
        get_event=AsyncMock(side_effect=get_event),
        validate_event_write_access=AsyncMock(side_effect=validate_event_write_access),
        list_calendars=AsyncMock(return_value={"calendars": [], "next_page_token": None}),
        delete_event=AsyncMock(return_value={"deleted": True}),
    )
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(calendar_agent_node, "CalendarService", lambda _db: cal_service)

    proposal_calls = []

    class FakeApprovalService:
        def __init__(self, _db):
            pass

        def create_proposal(self, **kw):
            proposal_calls.append(kw)
            payload = kw.get("payload")
            action_type = getattr(payload, "action_type", None) or (payload.get("action_type") if isinstance(payload, dict) else None)
            params = getattr(payload, "parameters", None) or (payload.get("parameters") if isinstance(payload, dict) else {})
            return SimpleNamespace(
                id=uuid4(),
                status="pending",
                model_dump=lambda mode="json": {
                    "id": str(uuid4()),
                    "status": "pending",
                    "action_type": action_type,
                    "parameters": params,
                },
            )

    monkeypatch.setattr(chat_service_module, "ActionApprovalService", FakeApprovalService)

    # Also provide calendar tools for tool-based calendar reads (e.g. upcoming events).
    class FakeTool:
        def __init__(self, name, handler):
            self.name = name
            self._handler = handler
            self.calls = []

        async def ainvoke(self, arguments):
            self.calls.append(arguments)
            return self._handler(arguments)

    def _jsonable_event(ev):
        e = json.loads(json.dumps(ev, default=str))
        return e

    def _env_ok(data):
        return json.dumps({"ok": True, "data": data}, default=str)

    calendar_tools = {
        "list_calendar_events": FakeTool(
            "list_calendar_events",
            lambda args: _env_ok({
                "events": [
                    _jsonable_event(e) for e in (
                        list(tenth_nov_events)
                        if str((args or {}).get("time_min", "")).startswith("2026-11-10")
                        or str((args or {}).get("time_min", "")).find("2026-11-10") != -1
                        else list(upcoming_events)
                    )
                ],
                "next_page_token": None,
                "calendar_id": (args or {}).get("calendar_id", "primary"),
            }),
        ),
        "list_calendars": FakeTool("list_calendars", lambda _a: _env_ok({"calendars": [], "next_page_token": None})),
        "get_calendar_event": FakeTool("get_calendar_event", lambda _a: _env_ok({})),
        "get_today_schedule": FakeTool("get_today_schedule", lambda _a: _env_ok({"events": [], "next_page_token": None, "calendar_id": "primary"})),
        "get_calendar_schedule": FakeTool("get_calendar_schedule", lambda args: _env_ok({"events": [], "next_page_token": None, "calendar_id": (args or {}).get("calendar_id", "primary"), "date": (args or {}).get("date")})),
    }

    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", lambda **_kw: list(calendar_tools.values()))

    gmail_exec_calls = []

    async def fake_execute_gmail_read(*, plan, **kwargs):
        gmail_exec_calls.append(plan)
        intent = getattr(plan, "intent", None) or (plan.get("intent") if isinstance(plan, dict) else None)
        if intent == "summarize_today":
            return {
                "messages": [],
                "period": "today",
                "search_criteria": "in:inbox newer_than:1d -from:me",
            }
        return {"messages": [], "period": "unknown", "search_criteria": ""}

    monkeypatch.setattr(gmail_agent_nodes, "execute_gmail_read", fake_execute_gmail_read, raising=False)

    contacts_calls = []

    def fake_generate_contacts_read_response(**kwargs):
        contacts_calls.append(kwargs)
        msg = (kwargs.get("message") or "").lower()
        if "mam" in msg:
            return {
                "assistant_response": "Mam's number: +8801700000001",
                "contacts_provenance": {"outcome_status": "single_match", "verified_source": True},
                "pending_contacts_clarification": None,
            }
        return {
            "assistant_response": "I couldn't find a matching contact.",
            "contacts_provenance": {"outcome_status": "no_match"},
            "pending_contacts_clarification": None,
        }

    monkeypatch.setattr(
        __import__("app.workflows.contacts_agent_node", fromlist=["generate_contacts_read_response"]),
        "generate_contacts_read_response",
        fake_generate_contacts_read_response,
    )

    gmail_agent_invocations = []
    orig_gmail_agent = gmail_agent_nodes.gmail_agent_node

    def gmail_agent_spy(state, runtime):
        gmail_agent_invocations.append({"intent": (state.get("gmail_plan") or {}).get("intent"), "msg": state.get("user_message")})
        return orig_gmail_agent(state, runtime)

    monkeypatch.setattr(gmail_agent_nodes, "gmail_agent_node", gmail_agent_spy)

    # Spy graph invocations to record intent/route/node/tool/pending per step
    graph_calls = []
    orig_invoke = chat_service_module.chat_graph.invoke

    def invoke_spy(state, *args, **kwargs):
        result = orig_invoke(state, *args, **kwargs)
        graph_calls.append({"state_in": state, "result": result})
        return result

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", invoke_spy)

    steps = [
        "Delete the event from 10th November calendar.",
        "Personal AI Assistant - Calendar Test",
        "check what emails I have for today",
        "whole day",
        "show me mams number",
        "show me my upcoming calendar events",
    ]
    responses = []
    step_snapshots = []
    for msg in steps:
        before = {
            "gmail_exec": len(gmail_exec_calls),
            "contacts": len(contacts_calls),
            "gmail_agent": len(gmail_agent_invocations),
            "list_events_service": len(calendar_calls["list_events"]),
            "list_events_tool": len(calendar_tools["list_calendar_events"].calls),
            "proposals": len(proposal_calls),
        }
        resp = _send(service, msg)
        responses.append(resp)
        after = {
            "gmail_exec": len(gmail_exec_calls),
            "contacts": len(contacts_calls),
            "gmail_agent": len(gmail_agent_invocations),
            "list_events_service": len(calendar_calls["list_events"]),
            "list_events_tool": len(calendar_tools["list_calendar_events"].calls),
            "proposals": len(proposal_calls),
        }
        step_snapshots.append({"before": before, "after": after})

    assert len(graph_calls) == 6
    assert len(responses) == 6
    debug_steps = [(i+1, gc["result"].get("assistant_route"), (gc["result"].get("gmail_plan") or {}).get("intent"), gc["result"].get("assistant_response")) for i,gc in enumerate(graph_calls)]
    _debug = {"steps": debug_steps, "gmail_exec_calls": gmail_exec_calls, "gmail_agent_invocations": gmail_agent_invocations, "step_snapshots": step_snapshots}
    try:
        with open("_debug_six_step.json", "w", encoding="utf-8") as f:
            json.dump(_debug, f, default=str, indent=2)
    except Exception:
        pass

    # Step 1: Calendar event discovery/deletion proposal (selection flow with candidates)
    s1_res = graph_calls[0]["result"]
    r1 = responses[0]
    assert s1_res.get("assistant_route") == "google_calendar", _debug
    assert ASSISTANT_ROUTE_NODES.get(s1_res.get("assistant_route")) == "calendar_agent", _debug
    assert (s1_res.get("gmail_plan") or {}).get("intent") == "google_calendar", _debug
    assert (
        any((c.get("calendar_id") == "primary" and c.get("time_min")) for c in calendar_calls["list_events"])
        or any((c.get("calendar_id") == "primary" and c.get("time_min")) for c in calendar_tools["list_calendar_events"].calls)
    ), _debug
    meta1 = r1["assistant_message"].get("metadata") or {}
    assert isinstance(meta1.get("pending_calendar_delete_selection"), dict), _debug
    assert meta1.get("pending_gmail_clarification") in (None, {}), _debug
    assert step_snapshots[0]["after"]["gmail_exec"] == step_snapshots[0]["before"]["gmail_exec"], _debug
    assert step_snapshots[0]["after"]["contacts"] == step_snapshots[0]["before"]["contacts"], _debug
    assert step_snapshots[0]["after"]["gmail_agent"] == step_snapshots[0]["before"]["gmail_agent"], _debug
    assert r1["assistant_message"]["chat_request_id"] == r1["user_message"]["chat_request_id"]
    assert r1["user_message"]["content"] == steps[0]

    # Step 2: Calendar title selection/continuation (resolves target event, prepares delete proposal, no Gmail)
    s2_res = graph_calls[1]["result"]
    r2 = responses[1]
    assert s2_res.get("assistant_route") == "google_calendar", _debug
    assert ASSISTANT_ROUTE_NODES.get(s2_res.get("assistant_route")) == "calendar_agent", _debug
    assert step_snapshots[1]["after"]["gmail_agent"] == step_snapshots[1]["before"]["gmail_agent"], _debug
    assert step_snapshots[1]["after"]["gmail_exec"] == step_snapshots[1]["before"]["gmail_exec"], _debug
    assert step_snapshots[1]["after"]["contacts"] == step_snapshots[1]["before"]["contacts"], _debug
    assert step_snapshots[1]["after"]["proposals"] >= step_snapshots[1]["before"]["proposals"] + 1, _debug
    created_payload = proposal_calls[-1].get("payload")
    created_params = getattr(created_payload, "parameters", None) or (created_payload.get("parameters") if isinstance(created_payload, dict) else {})
    assert created_params.get("event_id") == "evt-10nov-target", _debug
    assert created_params.get("calendar_id") == "primary", _debug
    meta2 = r2["assistant_message"].get("metadata") or {}
    assert (meta2.get("action_approval") or {}).get("action_type") == "calendar.delete_event", _debug
    assert meta2.get("pending_calendar_delete_selection") in (None, {}), _debug
    assert meta2.get("pending_gmail_clarification") in (None, {}), _debug
    assert r2["assistant_message"]["chat_request_id"] == r2["user_message"]["chat_request_id"]
    assert r2["user_message"]["content"] == steps[1]
    assert cal_service.delete_event.await_count == 0, _debug

    # Step 3: Gmail summarize_today (direct execution)
    s3_res = graph_calls[2]["result"]
    r3 = responses[2]
    assert s3_res.get("assistant_route") == "gmail", _debug
    assert ASSISTANT_ROUTE_NODES.get(s3_res.get("assistant_route")) == "gmail_agent", _debug
    assert (s3_res.get("gmail_plan") or {}).get("intent") == "summarize_today", _debug
    assert step_snapshots[2]["after"]["gmail_exec"] == step_snapshots[2]["before"]["gmail_exec"] + 1, _debug
    exec_intent = getattr(gmail_exec_calls[-1], "intent", None) or (gmail_exec_calls[-1].get("intent") if isinstance(gmail_exec_calls[-1], dict) else None)
    assert exec_intent == "summarize_today", _debug
    meta3 = r3["assistant_message"].get("metadata") or {}
    assert meta3.get("pending_gmail_clarification") in (None, {}), _debug
    assert r3["assistant_message"]["chat_request_id"] == r3["user_message"]["chat_request_id"]
    assert r3["user_message"]["content"] == steps[2]
    assert cal_service.delete_event.await_count == 0, _debug

    # Step 4: "whole day" must not invoke unrelated Calendar execution and must not invent Gmail clarification
    s4_res = graph_calls[3]["result"]
    r4 = responses[3]
    assert s4_res.get("assistant_route") != "google_calendar", _debug
    meta4 = r4["assistant_message"].get("metadata") or {}
    assert meta4.get("pending_gmail_clarification") in (None, {}), _debug
    assert cal_service.delete_event.await_count == 0, _debug
    # must not trigger additional gmail read execution as invented clarification continuation
    assert step_snapshots[3]["after"]["gmail_exec"] == step_snapshots[3]["before"]["gmail_exec"], _debug
    assert r4["assistant_message"]["chat_request_id"] == r4["user_message"]["chat_request_id"]
    assert r4["user_message"]["content"] == steps[3]

    # Step 5: Google Contacts read
    s5_res = graph_calls[4]["result"]
    r5 = responses[4]
    assert s5_res.get("assistant_route") == "google_contacts", _debug
    assert ASSISTANT_ROUTE_NODES.get(s5_res.get("assistant_route")) == "contacts_agent", _debug
    assert step_snapshots[4]["after"]["contacts"] >= step_snapshots[4]["before"]["contacts"] + 1, _debug
    assert "mam" in (r5["assistant_message"]["content"] or "").lower() or "+8801700000001" in (r5["assistant_message"]["content"] or ""), _debug
    meta5 = r5["assistant_message"].get("metadata") or {}
    assert meta5.get("pending_gmail_clarification") in (None, {}), _debug
    assert meta5.get("pending_contacts_clarification") in (None, {}), _debug
    assert r5["assistant_message"]["chat_request_id"] == r5["user_message"]["chat_request_id"]
    assert r5["user_message"]["content"] == steps[4]

    # Step 6: Fresh Calendar upcoming-event discovery/read
    s6_res = graph_calls[5]["result"]
    r6 = responses[5]
    assert s6_res.get("assistant_route") == "google_calendar", _debug
    assert ASSISTANT_ROUTE_NODES.get(s6_res.get("assistant_route")) == "calendar_agent", _debug
    assert (
        (step_snapshots[5]["after"]["list_events_service"] >= step_snapshots[5]["before"]["list_events_service"] + 1)
        or (step_snapshots[5]["after"]["list_events_tool"] >= step_snapshots[5]["before"]["list_events_tool"] + 1)
    ), _debug
    meta6 = r6["assistant_message"].get("metadata") or {}
    assert meta6.get("pending_gmail_clarification") in (None, {}), _debug
    assert r6["assistant_message"]["chat_request_id"] == r6["user_message"]["chat_request_id"]
    assert r6["user_message"]["content"] == steps[5]

    # Cross-conversation/user isolation and no actual provider mutation
    assert all(m.get("conversation_id") == CONVERSATION_ID for m in message_service.messages)
    assert all(r["conversation_id"] == CONVERSATION_ID for r in responses)
    assert cal_service.delete_event.await_count == 0, _debug
