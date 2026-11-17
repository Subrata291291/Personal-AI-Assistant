from uuid import uuid4

from app.workflows.contacts_tools import create_contacts_tools


def test_contacts_tools_are_read_only():
    tools = create_contacts_tools(str(uuid4()), str(uuid4()))
    names = {tool.name for tool in tools}
    assert names == {"list_contacts", "search_contacts", "get_contact"}
    assert "create_contact" not in names
    assert "update_contact" not in names
    assert "delete_contact" not in names


def test_search_contacts_tool_schema_supports_page_token():
    tools = {t.name: t for t in create_contacts_tools(str(uuid4()), str(uuid4()))}
    schema = tools["search_contacts"].args_schema
    assert "page_token" in schema.model_fields
    assert schema.model_fields["page_token"].annotation in (str | None, type(None) | str) or "Optional" in str(schema.model_fields["page_token"].annotation)