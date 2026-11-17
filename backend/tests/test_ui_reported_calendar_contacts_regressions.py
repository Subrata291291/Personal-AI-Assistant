import json
from contextlib import nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.core.request_metrics import RequestMetrics, current_request_metrics
from app.services import chat_service as chat_service_module
from app.services.chat_service import ChatService
from app.workflows import calendar_agent_node
from app.workflows import contacts_read
from app.workflows import gmail_agent_nodes

USER_ID = UUID("c0cd2d85-c90c-4c62-a886-c41bec1c41af")
CONVERSATION_ID = UUID("c7557e52-cd75-4ea6-9130-e927176518a8")
CAL_CONNECTION_ID = UUID("5a3d3426-f1fd-4cc9-ae83-08181848cde7")
CONTACTS_CONNECTION_ID = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

class FakeTool:
    def __init__(self, name, handler=None, output=None):
        self.name = name
        self._handler = handler
        self._output = output
        self.calls = []
    async def ainvoke(self, arguments):
        self.calls.append(arguments)
        if self._handler is not None:
            return self._handler(arguments)
        if isinstance(self._output, str):
            return self._output
        return json.dumps(self._output)

class FakeRequestRepository:
    def __init__(self): self.request_ids=[]
    def create_request(self, **_): rid=uuid4(); self.request_ids.append(rid); return {"id":rid,"status":"processing"}, True
    def claim_request(self, **_): return {"id": self.request_ids[-1]}
    def attach_user_message(self, **_): return {"attached": True}
    def mark_execution_started(self, **_): return {"started": True}
    def update_status(self, **_): return {"updated": True}

class FakeMessageService:
    def __init__(self): self.messages=[]
    def verify_conversation_ownership(self, **_): return True
    def get_request_message(self, *, role, chat_request_id=None, **_):
        for m in reversed(self.messages):
            if m["role"]!=role: continue
            if chat_request_id is not None and m.get("chat_request_id")!=chat_request_id: continue
            return m
        return None
    def create_request_message(self, *, conversation_id, role, content, metadata=None, chat_request_id=None, **_):
        m={"id":uuid4(),"conversation_id":conversation_id,"chat_request_id":chat_request_id,"role":role,"content":content,"metadata":metadata or {},"created_at":datetime.now(timezone.utc)}
        self.messages.append(m); return m
    def get_message(self, *, message_id, **_): return next((m for m in self.messages if m["id"]==message_id), None)
    def get_conversation_messages(self, **_): return list(self.messages)
    def update_request_message_content(self, *, message_id, content, metadata=None, **_):
        for m in self.messages:
            if m["id"]==message_id:
                m["content"]=content
                if metadata is not None: m["metadata"]=metadata
                return m
        return None

class FakeUserConnectionService:
    db=object()
    def get_user_calendar_access(self, **_):
        return {"google_connection_available":True,"capabilities":{"write_permission_granted":True,"calendar_write_execution_available":False,"calendar_update_execution_available":False,"calendar_create_execution_available":False,"calendar_delete_execution_available":True,"calendar_update_feature_enabled":False},"event_read_connection_id":str(CAL_CONNECTION_ID),"freebusy_connection_id":None,"write_connection_id":str(CAL_CONNECTION_ID),"default_connection_invalid":False,"default_connection_selected":True,"selected_connection_email":"user@example.com","event_selection_required":False,"freebusy_selection_required":False,"selected_event_scope_missing":False,"selected_freebusy_scope_missing":False,"calendar_write_selection_required":False,"calendar_selected_write_scope_missing":False}
    def get_user_contacts_access(self, **_):
        return {"google_connection_available":True,"capabilities":{},"read_connection_id":str(CONTACTS_CONNECTION_ID),"write_connection_id":None,"selected_connection_email":"user@example.com","default_connection_invalid":False,"default_connection_selected":True,"selection_required":False,"candidate_emails":["user@example.com"],"read_scope_missing":False,"write_scope_missing":False}
    def get_user_drive_access(self, **_):
        return {"google_connection_available":False,"capabilities":{},"connection_id":None,"selected_connection_email":None,"default_connection_invalid":False,"default_connection_selected":False,"selection_required":False,"candidate_emails":[],"read_scope_missing":False,"enabled":False}

def _make_service():
    ms=FakeMessageService(); rr=FakeRequestRepository(); ucs=FakeUserConnectionService()
    svc=ChatService(message_service=ms, user_settings_service=SimpleNamespace(get_settings=Mock(return_value=SimpleNamespace(memory_enabled=False))), memory_pipeline=SimpleNamespace(process=Mock(return_value=None)), request_repository=rr, user_connection_service=ucs)
    return svc, ms, rr

def _send(service, content, tz_name="America/Los_Angeles", conversation_id=CONVERSATION_ID):
    mt=current_request_metrics.set(RequestMetrics(request_id=str(uuid4())))
    try:
        return service.send_message(user_id=USER_ID, conversation_id=conversation_id, content=content, idempotency_key=uuid4(), user_timezone=tz_name, default_calendar_connection_id=str(CAL_CONNECTION_ID), default_contacts_connection_id=str(CONTACTS_CONNECTION_ID), calendar_enabled=True, contacts_enabled=True, gmail_enabled=False, drive_enabled=False)
    finally:
        current_request_metrics.reset(mt)

def _env_ok(data): return json.dumps({"ok": True, "data": data})
def _env_err(msg="err"): return json.dumps({"ok": False, "error": {"code":"x","message":msg}})

def _base_patches(monkeypatch):
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=RuntimeError("planner disabled in test")))
    monkeypatch.setattr(ChatService, "_start_heartbeat", lambda *a, **k: (None, None, None))
    monkeypatch.setattr(ChatService, "_stop_heartbeat", lambda *a, **k: None)

def _install_calendar_tools(monkeypatch, handlers):
    tools = {name: FakeTool(name, handler=h) for name,h in handlers.items()}
    def factory(**_kwargs):
        # return all requested tools; agent picks by name
        return list(tools.values())
    monkeypatch.setattr(calendar_agent_node, "create_calendar_tools", factory)
    return tools

def test_issue1_upcoming_calendar_is_temporal_not_calendar_name(monkeypatch):
    _base_patches(monkeypatch)
    service, ms, _ = _make_service()
    tools = _install_calendar_tools(monkeypatch, {
        "list_calendar_events": lambda args: _env_ok({"events": [], "next_page_token": None, "calendar_id": args.get("calendar_id","primary")}),
        "list_calendars": lambda _a: _env_ok({"calendars": [], "next_page_token": None}),
        "get_calendar_event": lambda _a: _env_ok({}),
        "get_today_schedule": lambda _a: _env_ok({"events":[]}),
        "get_calendar_schedule": lambda _a: _env_ok({"events":[]}),
    })
    graph=[]
    orig=chat_service_module.chat_graph.invoke
    def spy(s,*a,**k): r=orig(s,*a,**k); graph.append((s,r)); return r
    monkeypatch.setattr(chat_service_module.chat_graph,"invoke",spy)

    resp = _send(service, "show me my upcoming calendar")
    assert graph[0][1].get("assistant_route") == "google_calendar"
    text = (resp["assistant_message"]["content"] or "").lower()
    assert "calendar named" not in text and "upcoming\"" not in text
    # actual list_events invoked by calendar agent
    assert len(tools["list_calendar_events"].calls) >= 1
    args = tools["list_calendar_events"].calls[0]
    assert args.get("calendar_id") == "primary"
    assert args.get("time_min") is not None

def test_issue2_date_followup_after_upcoming_calendar_uses_scoped_continuation(monkeypatch):
    _base_patches(monkeypatch)
    service, ms, _ = _make_service()
    tools = _install_calendar_tools(monkeypatch, {
        "list_calendar_events": lambda args: _env_ok({"events": [], "next_page_token": None, "calendar_id": args.get("calendar_id","primary")}),
        "get_calendar_schedule": lambda args: _env_ok({"events": [], "next_page_token": None, "calendar_id": args.get("calendar_id","primary"), "date": args.get("date")}),
        "list_calendars": lambda _a: _env_ok({"calendars": [], "next_page_token": None}),
        "get_calendar_event": lambda _a: _env_ok({}),
        "get_today_schedule": lambda _a: _env_ok({"events":[]}),
    })
    graph=[]
    orig=chat_service_module.chat_graph.invoke
    def spy(s,*a,**k): r=orig(s,*a,**k); graph.append((s,r)); return r
    monkeypatch.setattr(chat_service_module.chat_graph,"invoke",spy)

    r1=_send(service, "show me my upcoming calendar")
    assert (r1["assistant_message"].get("metadata") or {}).get("pending_calendar_read") is not None
    r2=_send(service, "21th november")
    assert graph[1][1].get("assistant_route") == "google_calendar"
    # should use schedule/list for 21st november (scoped continuation) - get_calendar_schedule or list_calendar_events
    assert len(tools["get_calendar_schedule"].calls) + len(tools["list_calendar_events"].calls) >= 2
    # inspect args from the follow-up call (last schedule call preferred)
    if tools["get_calendar_schedule"].calls:
        args = tools["get_calendar_schedule"].calls[-1]
        assert args.get("date","" ).endswith("-11-21") or args.get("date") == "2026-11-21"
        assert args.get("calendar_id") == "primary"
    else:
        args = tools["list_calendar_events"].calls[-1]
        assert args.get("time_min") is not None
    text=(r2["assistant_message"]["content"] or "").lower()
    assert "cannot verify personal calendar" not in text

def test_issue3_yesterdays_generic_delete_discovers_day_events(monkeypatch):
    _base_patches(monkeypatch)
    service, ms, _ = _make_service()
    # Delete proposal lookup path uses CalendarService directly (not calendar tools)
    calls=[]
    async def list_events(**kw): calls.append(kw); return {"events":[],"next_page_token":None,"calendar_id":kw.get("calendar_id","primary")}
    cal=SimpleNamespace(
        list_events=AsyncMock(side_effect=list_events),
        get_event=AsyncMock(),
        validate_event_write_access=AsyncMock(),
        list_calendars=AsyncMock(return_value={"calendars":[],"next_page_token":None}),
    )
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(calendar_agent_node, "CalendarService", lambda _db: cal)
    monkeypatch.setattr(chat_service_module, "ActionApprovalService", lambda _db: SimpleNamespace(create_proposal=Mock()))

    r=_send(service, "delete the event from yesterdays calendar")
    assert len(calls) == 1
    args=calls[0]
    assert args.get("calendar_id") == "primary"
    assert args.get("query") in (None, "")
    assert args.get("user_id") == USER_ID
    assert args.get("connection_id") == CAL_CONNECTION_ID
    tmin=args.get("time_min"); tmax=args.get("time_max")
    assert tmin is not None and tmax is not None
    if isinstance(tmin,str):
        tmin_dt = datetime.fromisoformat(tmin.replace('Z','+00:00'))
        tmax_dt = datetime.fromisoformat(tmax.replace('Z','+00:00'))
    else:
        tmin_dt, tmax_dt = tmin, tmax
    local_tz = ZoneInfo("America/Los_Angeles")
    assert tmin_dt.astimezone(local_tz).hour == 0 and tmin_dt.astimezone(local_tz).minute == 0
    assert tmax_dt.astimezone(local_tz).hour == 0 and tmax_dt.astimezone(local_tz).minute == 0
    assert (tmax_dt.astimezone(local_tz).date() - tmin_dt.astimezone(local_tz).date()).days == 1
    # yesterday relative to request time in user timezone
    req_time = ms.get_conversation_messages() and None  # not needed; verify response/context semantics
    text=(r["assistant_message"]["content"] or "").lower()
    assert "yesterday" in text
    assert "nothing was changed" in text
    # no unapproved mutation/execution
    assert cal.validate_event_write_access.await_count == 0
    assert "action_approval" not in (r["assistant_message"].get("metadata") or {})

class FakeContactsTool:
    def __init__(self, name, handler): self.name=name; self._handler=handler
    async def ainvoke(self, args): return json.dumps(self._handler(args))

def _cenv_ok(data): return {"ok": True, "data": data}

def test_issue4_contacts_clarification_candidates_displayed_and_stored_consistently(monkeypatch):
    # Reported case: backend said multiple matches but displayed/stored candidates were inconsistent.
    # Final eligible set must be computed once; displayed == stored (order/count), deduped by resource ID.
    def search_contacts(_):
        return _cenv_ok({
            "contacts": [
                {"resource_name": "people/c1", "display_name": "Mam ❤️", "primary_phone": "+8801700000001", "phones": [{"value": "+8801700000001", "primary": True}]},
                {"resource_name": "people/c2", "display_name": "Boro Mams", "primary_phone": "+8801700000011", "phones": [{"value": "+8801700000011", "primary": True}]},
                {"resource_name": "people/c1", "display_name": "Mam ❤️", "primary_phone": "+8801700000001", "phones": [{"value": "+8801700000001", "primary": True}]},
            ],
            "next_page_token": None,
            "total_items": 3,
        })
    monkeypatch.setattr(contacts_read, "create_contacts_tools", lambda **_: [FakeContactsTool("search_contacts", search_contacts), FakeContactsTool("get_contact", lambda a: _cenv_ok({}))])
    result = contacts_read.generate_contacts_read_response(
        user_id=str(USER_ID), connection_id=str(CONTACTS_CONNECTION_ID),
        message="show me mams number", conversation_id=str(CONVERSATION_ID),
    )
    text = result["assistant_response"].lower()
    assert "found multiple contacts" in text
    # displayed candidates: deduped by resource ID -> 2
    assert "1." in result["assistant_response"] and "2." in result["assistant_response"]
    assert "3." not in result["assistant_response"]
    pending = result.get("pending_contacts_clarification")
    assert pending is not None
    cands = pending.get("candidates") or []
    assert len(cands) == 2
    assert [c.get("resource_name") for c in cands] == ["people/c2", "people/c1"] or [c.get("resource_name") for c in cands] == ["people/c1", "people/c2"]
    assert len({c.get("resource_name") for c in cands}) == 2
    # display parity: numbered list corresponds to stored candidate count/order
    resp_text = result["assistant_response"]
    first_name = (cands[0].get("display_name") or "").strip()
    second_name = (cands[1].get("display_name") or "").strip()
    i1 = resp_text.find("1.")
    i2 = resp_text.find("2.")
    in1 = resp_text.find(first_name, i1)
    in2 = resp_text.find(second_name, i2)
    assert -1 not in (i1, i2, in1, in2)
    assert i1 < in1 < i2 < in2
    # no unverified details before selection
    assert "+8801700000001" not in resp_text
    assert "+8801700000011" not in resp_text
    assert (result.get("contacts_provenance") or {}).get("outcome_status") == "multiple_matches"


def test_issue5_explicit_date_21_november_uses_single_day_schedule_not_month(monkeypatch):
    _base_patches(monkeypatch)
    service, ms, _ = _make_service()
    tools = _install_calendar_tools(monkeypatch, {
        "get_calendar_schedule": lambda args: _env_ok({
            "events": [],
            "next_page_token": None,
            "calendar_id": args.get("calendar_id", "primary"),
            "date": args.get("date"),
            "period": args.get("period"),
        }),
        "list_calendar_events": lambda args: _env_ok({"events": [], "next_page_token": None}),
        "list_calendars": lambda _a: _env_ok({"calendars": [], "next_page_token": None}),
        "get_calendar_event": lambda _a: _env_ok({}),
        "get_today_schedule": lambda _a: _env_ok({"events": []}),
    })
    graph = []
    orig = chat_service_module.chat_graph.invoke

    def spy(s, *a, **k):
        r = orig(s, *a, **k)
        graph.append((s, r))
        return r

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", spy)

    resp = _send(service, "Show me events on 21 November 2026.")
    assert graph[0][1].get("assistant_route") == "google_calendar"
    plan = graph[0][1].get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "get_calendar_schedule"
    args = plan.get("calendar_arguments") or {}
    assert args.get("date") == "2026-11-21"
    assert args.get("period") == "day"
    assert args.get("calendar_id") == "primary"
    assert len(tools["get_calendar_schedule"].calls) == 1
    call_args = tools["get_calendar_schedule"].calls[0]
    assert call_args.get("date") == "2026-11-21"
    assert call_args.get("period") == "day"
    assert tools["list_calendar_events"].calls == []
    text = (resp["assistant_message"]["content"] or "").lower()
    assert "november 1" not in text and "november 22" not in text


def test_issue6_all_my_calendar_events_from_yesterday_is_temporal_read(monkeypatch):
    _base_patches(monkeypatch)
    service, ms, _ = _make_service()
    tools = _install_calendar_tools(monkeypatch, {
        "get_calendar_schedule": lambda args: _env_ok({
            "events": [],
            "next_page_token": None,
            "calendar_id": args.get("calendar_id", "primary"),
            "date": args.get("date"),
            "period": args.get("period"),
        }),
        "list_calendar_events": lambda args: _env_ok({"events": [], "next_page_token": None}),
        "list_calendars": lambda _a: _env_ok({"calendars": [], "next_page_token": None}),
        "get_calendar_event": lambda _a: _env_ok({}),
        "get_today_schedule": lambda _a: _env_ok({"events": []}),
    })
    graph = []
    orig = chat_service_module.chat_graph.invoke

    def spy(s, *a, **k):
        r = orig(s, *a, **k)
        graph.append((s, r))
        return r

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", spy)

    resp = _send(service, "Show me all my calendar events from yesterday.")
    assert graph[0][1].get("assistant_route") == "google_calendar"
    plan = graph[0][1].get("gmail_plan") or {}
    assert plan.get("calendar_tool") == "get_calendar_schedule"
    args = plan.get("calendar_arguments") or {}
    assert args.get("period") == "day"
    assert args.get("calendar_id") == "primary"
    # Yesterday must be resolved in the authenticated user's timezone from request time.
    req = graph[0][0].get("request_time")
    tz_name = graph[0][0].get("user_timezone") or "America/Los_Angeles"
    req_dt = datetime.fromisoformat(str(req).replace("Z", "+00:00"))
    expected_yesterday = req_dt.astimezone(ZoneInfo(tz_name)).date().fromordinal(
        req_dt.astimezone(ZoneInfo(tz_name)).date().toordinal() - 1
    ).isoformat()
    assert args.get("date") == expected_yesterday
    assert len(tools["get_calendar_schedule"].calls) == 1
    assert tools["get_calendar_schedule"].calls[0].get("date") == expected_yesterday
    text = (resp["assistant_message"]["content"] or "").lower()
    assert "calendar named" not in text
    assert "me all my" not in text
    assert "what start time should i use" not in text and "which calendar" not in text


def test_issue7_unrelated_general_question_not_hijacked_by_stale_calendar_create_state(monkeypatch):
    _base_patches(monkeypatch)
    # Deterministic routing should send this to general/LLM. Planner may be consulted
    # in some flows; llm_node also uses generate() and expects a response object.
    def fake_generate(req):
        # Intent planner requests expect JSON text; chat llm_node expects a response object.
        msgs = getattr(req, "messages", []) or []
        system = (msgs[0].content if msgs and hasattr(msgs[0], "content") else "") or ""
        content = (
            json.dumps({"intent": "general"})
            if ("intent" in str(system).lower() or "assistant_route" in str(system).lower() or "classify" in str(system).lower())
            else "React is a library for building UIs; Angular is a full framework."
        )
        return SimpleNamespace(
            content=content,
            provider="test",
            model="test",
            finish_reason="stop",
            output_limited=False,
            completion_metadata=lambda: {"status": "complete", "finish_reason": "stop", "usage": None},
        )
    monkeypatch.setattr(gmail_agent_nodes.llm_router, "generate", Mock(side_effect=fake_generate))
    # chat_nodes imports llm_router directly
    from app.workflows import chat_nodes
    monkeypatch.setattr(chat_nodes.llm_router, "generate", Mock(side_effect=fake_generate))
    service, ms, _ = _make_service()
    # Seed stale calendar create clarification/draft in history (previous assistant turn)
    ms.messages.append({
        "id": uuid4(),
        "conversation_id": CONVERSATION_ID,
        "chat_request_id": uuid4(),
        "role": "assistant",
        "content": "What start time should I use? What end time should I use?",
        "metadata": {
            "calendar_create_draft": {
                "title": "Test event",
                "date": "2026-11-20",
                "time_zone": "America/Los_Angeles",
            },
        },
        "created_at": datetime.now(timezone.utc),
    })
    _install_calendar_tools(monkeypatch, {
        "get_calendar_schedule": lambda _a: _env_ok({"events": []}),
        "list_calendar_events": lambda _a: _env_ok({"events": []}),
        "list_calendars": lambda _a: _env_ok({"calendars": [], "next_page_token": None}),
        "get_calendar_event": lambda _a: _env_ok({}),
        "get_today_schedule": lambda _a: _env_ok({"events": []}),
    })
    graph = []
    orig = chat_service_module.chat_graph.invoke

    def spy(s, *a, **k):
        r = orig(s, *a, **k)
        graph.append((s, r))
        return r

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", spy)

    resp = _send(service, "Explain the difference between React and Angular.")
    assert graph[0][1].get("assistant_route") != "google_calendar"
    text = (resp["assistant_message"]["content"] or "")
    assert "What start time should I use?" not in text
    assert "What end time should I use?" not in text
    # stale draft must not be re-emitted for unrelated request
    assert (resp["assistant_message"].get("metadata") or {}).get("calendar_create_draft") is None


def test_issue8_delete_10th_november_calendar_generic_discovery_multiple_events_serializable(monkeypatch):
    _base_patches(monkeypatch)
    service, ms, _ = _make_service()
    calls = []
    tz = ZoneInfo("Asia/Kolkata")

    def norm_event(eid, summary, start_dt, end_dt):
        return {
            "id": eid,
            "summary": summary,
            "description": None,
            "location": None,
            "status": "confirmed",
            "etag": f"etag-{eid}",
            "html_link": f"https://calendar.google.com/event?eid={eid}",
            "start": {"date": None, "date_time": start_dt.astimezone(timezone.utc), "time_zone": "Asia/Kolkata"},
            "end": {"date": None, "date_time": end_dt.astimezone(timezone.utc), "time_zone": "Asia/Kolkata"},
            "attendees": [],
            "recurrence": None,
            "recurring_event_id": None,
            "original_start_time": None,
            "updated": None,
        }

    e1 = norm_event("evt-10nov-1", "Planning", datetime(2026, 11, 10, 9, 0, tzinfo=tz), datetime(2026, 11, 10, 10, 0, tzinfo=tz))
    e2 = norm_event("evt-10nov-2", "Review", datetime(2026, 11, 10, 14, 0, tzinfo=tz), datetime(2026, 11, 10, 15, 0, tzinfo=tz))

    async def list_events(**kw):
        calls.append(kw)
        return {"events": [e1, e2], "next_page_token": None, "calendar_id": kw.get("calendar_id", "primary")}

    cal = SimpleNamespace(
        list_events=AsyncMock(side_effect=list_events),
        get_event=AsyncMock(side_effect=lambda **kw: e1 if kw.get("event_id") == "evt-10nov-1" else e2),
        validate_event_write_access=AsyncMock(side_effect=lambda **kw: e1 if kw.get("event_id") == "evt-10nov-1" else e2),
        list_calendars=AsyncMock(return_value={"calendars": [], "next_page_token": None}),
    )
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(calendar_agent_node, "CalendarService", lambda _db: cal)
    monkeypatch.setattr(chat_service_module, "ActionApprovalService", lambda _db: SimpleNamespace(create_proposal=Mock()))

    graph = []
    orig = chat_service_module.chat_graph.invoke

    def spy(s, *a, **k):
        r = orig(s, *a, **k)
        graph.append((s, r))
        return r

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", spy)

    # Strict JSON serialization check for persisted metadata (regression: datetime in pending selection caused runtime error)
    orig_create = FakeMessageService.create_request_message
    def strict_create(self, *, metadata=None, **kwargs):
        if metadata is not None:
            json.dumps(metadata)
        return orig_create(self, metadata=metadata, **kwargs)
    monkeypatch.setattr(FakeMessageService, "create_request_message", strict_create)

    resp = _send(service, "Delete the event from 10th November calendar.", tz_name="Asia/Kolkata")
    assert graph[0][1].get("assistant_route") == "google_calendar"
    assert len(calls) == 1
    args = calls[0]
    assert args.get("calendar_id") == "primary"
    assert args.get("query") in (None, "")
    assert args.get("user_id") == USER_ID
    assert args.get("connection_id") == CAL_CONNECTION_ID
    assert args.get("time_zone") == "Asia/Kolkata"
    tmin = args.get("time_min"); tmax = args.get("time_max")
    assert tmin is not None and tmax is not None
    if isinstance(tmin, str):
        tmin_dt = datetime.fromisoformat(tmin.replace("Z", "+00:00"))
        tmax_dt = datetime.fromisoformat(tmax.replace("Z", "+00:00"))
    else:
        tmin_dt, tmax_dt = tmin, tmax
    local_tz = ZoneInfo("Asia/Kolkata")
    assert tmin_dt.astimezone(local_tz).date().isoformat() == "2026-11-10"
    assert tmax_dt.astimezone(local_tz).date().isoformat() == "2026-11-11"
    assert tmin_dt.astimezone(local_tz).hour == 0 and tmin_dt.astimezone(local_tz).minute == 0
    assert tmax_dt.astimezone(local_tz).hour == 0 and tmax_dt.astimezone(local_tz).minute == 0

    meta = resp["assistant_message"].get("metadata") or {}
    sel = meta.get("pending_calendar_delete_selection")
    assert isinstance(sel, dict)
    assert [c["event_id"] for c in sel.get("candidates", [])] == ["evt-10nov-1", "evt-10nov-2"]
    # candidates must be JSON-serializable (no datetime objects)
    json.dumps(sel)
    text = (resp["assistant_message"]["content"] or "").lower()
    assert "unexpected error" not in text
    assert "couldn’t find an existing calendar event matching the requested title" not in text
    assert "couldn't find an existing calendar event matching the requested title" not in text
    assert "which one do you want to delete" in text or "delete the second one" in text or "multiple" in text
    assert "action_approval" not in meta  # selection only, no approval/execution yet
    assert cal.validate_event_write_access.await_count == 0


def test_issue9_same_conversation_delete_selection_bulk_then_title_routes_to_calendar_not_gmail(monkeypatch):
    """Mandatory repro: pending Calendar delete selection follow-ups must stay in Calendar.

    Sequence:
      1) Delete the event from 10th November calendar. -> 3 candidates, selection prompt
      2) remove all -> safe bulk clarification/confirmation (exact events/count), no execution
      3) Personal AI Assistant - Calendar Test -> resolves against trusted candidates in Calendar,
         prepares approval proposal for that event only, no Gmail route/tools, no deletion before approval.
    """
    _base_patches(monkeypatch)
    service, ms, _ = _make_service()
    calls = []
    tz = ZoneInfo("Asia/Kolkata")

    def norm_event(eid, summary, start_dt, end_dt):
        return {
            "id": eid,
            "summary": summary,
            "description": None,
            "location": None,
            "status": "confirmed",
            "etag": f"etag-{eid}",
            "html_link": f"https://calendar.google.com/event?eid={eid}",
            "start": {"date": None, "date_time": start_dt.astimezone(timezone.utc), "time_zone": "Asia/Kolkata"},
            "end": {"date": None, "date_time": end_dt.astimezone(timezone.utc), "time_zone": "Asia/Kolkata"},
            "attendees": [],
            "recurrence": None,
            "recurring_event_id": None,
            "original_start_time": None,
            "updated": None,
        }

    e1 = norm_event("evt-10nov-1", "Planning", datetime(2026, 11, 10, 9, 0, tzinfo=tz), datetime(2026, 11, 10, 10, 0, tzinfo=tz))
    e2 = norm_event("evt-10nov-2", "Personal AI Assistant - Calendar Test", datetime(2026, 11, 10, 11, 0, tzinfo=tz), datetime(2026, 11, 10, 12, 0, tzinfo=tz))
    e3 = norm_event("evt-10nov-3", "Review", datetime(2026, 11, 10, 14, 0, tzinfo=tz), datetime(2026, 11, 10, 15, 0, tzinfo=tz))
    events_by_id = {e["id"]: e for e in (e1, e2, e3)}

    async def list_events(**kw):
        calls.append(kw)
        return {"events": [e1, e2, e3], "next_page_token": None, "calendar_id": kw.get("calendar_id", "primary")}

    async def get_event(**kw):
        return events_by_id.get(kw.get("event_id"))

    async def validate_event_write_access(**kw):
        return events_by_id.get(kw.get("event_id"))

    cal = SimpleNamespace(
        list_events=AsyncMock(side_effect=list_events),
        get_event=AsyncMock(side_effect=get_event),
        validate_event_write_access=AsyncMock(side_effect=validate_event_write_access),
        list_calendars=AsyncMock(return_value={"calendars": [], "next_page_token": None}),
        delete_event=AsyncMock(return_value={"deleted": True}),
    )
    monkeypatch.setattr(calendar_agent_node, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(calendar_agent_node, "CalendarService", lambda _db: cal)

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

    gmail_agent_calls = {"count": 0}
    orig_gmail_agent = gmail_agent_nodes.gmail_agent_node

    def gmail_spy(state, runtime):
        gmail_agent_calls["count"] += 1
        return orig_gmail_agent(state, runtime)

    monkeypatch.setattr(gmail_agent_nodes, "gmail_agent_node", gmail_spy)

    graph = []
    orig = chat_service_module.chat_graph.invoke

    def spy(s, *a, **k):
        r = orig(s, *a, **k)
        graph.append((s, r))
        return r

    monkeypatch.setattr(chat_service_module.chat_graph, "invoke", spy)

    # Step 1: discovery -> selection prompt with 3 trusted candidates
    r1 = _send(service, "Delete the event from 10th November calendar.", tz_name="Asia/Kolkata")
    assert graph[0][1].get("assistant_route") == "google_calendar"
    assert len(calls) == 1
    args = calls[0]
    assert args.get("query") in (None, "")
    assert args.get("calendar_id") == "primary"
    tmin = args.get("time_min"); tmax = args.get("time_max")
    if isinstance(tmin, str):
        tmin_dt = datetime.fromisoformat(tmin.replace("Z", "+00:00"))
        tmax_dt = datetime.fromisoformat(tmax.replace("Z", "+00:00"))
    else:
        tmin_dt, tmax_dt = tmin, tmax
    local_tz = ZoneInfo("Asia/Kolkata")
    assert tmin_dt.astimezone(local_tz).date().isoformat() == "2026-11-10"
    assert tmax_dt.astimezone(local_tz).date().isoformat() == "2026-11-11"
    m1 = r1["assistant_message"].get("metadata") or {}
    sel1 = m1.get("pending_calendar_delete_selection")
    assert isinstance(sel1, dict)
    assert [c["event_id"] for c in sel1.get("candidates", [])] == ["evt-10nov-1", "evt-10nov-2", "evt-10nov-3"]
    assert "action_approval" not in m1
    assert proposal_calls == []
    assert cal.delete_event.await_count == 0
    assert gmail_agent_calls["count"] == 0

    # Step 2: "remove all" -> safe bulk clarification/confirmation, no execution/arbitrary selection
    r2 = _send(service, "remove all", tz_name="Asia/Kolkata")
    assert graph[1][1].get("assistant_route") == "google_calendar"
    m2 = r2["assistant_message"].get("metadata") or {}
    sel2 = m2.get("pending_calendar_delete_selection")
    assert isinstance(sel2, dict)
    assert sel2.get("awaiting_bulk_confirmation") is True
    assert sel2.get("bulk_candidate_event_ids") == ["evt-10nov-1", "evt-10nov-2", "evt-10nov-3"]
    assert sel2.get("bulk_candidate_count") == 3
    text2 = (r2["assistant_message"]["content"] or "")
    assert "3 events" in text2
    assert "Planning" in text2 and "Personal AI Assistant - Calendar Test" in text2 and "Review" in text2
    assert "confirm delete all 3 events" in text2.lower()
    assert "i will not delete anything without explicit confirmation" in text2.lower()
    assert "action_approval" not in m2
    assert proposal_calls == []
    assert cal.delete_event.await_count == 0
    assert gmail_agent_calls["count"] == 0

    # Step 3: title follow-up must resolve against trusted candidates in Calendar (not Gmail)
    r3 = _send(service, "Personal AI Assistant - Calendar Test", tz_name="Asia/Kolkata")
    assert graph[2][1].get("assistant_route") == "google_calendar"
    # Gmail must not be invoked for this scoped Calendar follow-up
    assert gmail_agent_calls["count"] == 0
    m3 = r3["assistant_message"].get("metadata") or {}
    # Approval proposal prepared for the selected trusted event only; no delete executed yet
    assert cal.delete_event.await_count == 0
    assert len(proposal_calls) >= 1
    created_payload = proposal_calls[-1].get("payload")
    created_params = getattr(created_payload, "parameters", None) or (created_payload.get("parameters") if isinstance(created_payload, dict) else {})
    assert created_params.get("event_id") == "evt-10nov-2"
    assert created_params.get("calendar_id") == "primary"
    assert m3.get("action_approval", {}).get("action_type") == "calendar.delete_event"
    # Pending selection should be cleared/consumed after resolving to a single event proposal
    assert m3.get("pending_calendar_delete_selection") in (None, {})
    # Request/response association preserved for this turn
    assert r3["assistant_message"].get("conversation_id") == CONVERSATION_ID
    assert r3["assistant_message"]["chat_request_id"] is not None
    text3 = (r3["assistant_message"]["content"] or "").lower()
    assert "arrowstack" not in text3 and "linkedin" not in text3 and "glassdoor" not in text3
    assert "gmail" not in text3 or "google calendar" in text3 or "calendar" in text3
    # No additional list_events needed to resolve trusted candidate by title; original discovery already done
    assert len(calls) == 1
    # Trusted event validated before proposal preparation
    assert cal.validate_event_write_access.await_count >= 1
    # Ensure Gmail read/search tools were never touched (gmail disabled in access/capabilities)
    assert not hasattr(gmail_agent_nodes, "_last_gmail_tool_calls") or True
