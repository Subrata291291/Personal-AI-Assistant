from typing import Any
from uuid import UUID

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.services.contacts_service import ContactsService
from app.db.session import SessionLocal


class ListContactsInput(BaseModel):
    page_size: int = Field(default=100, ge=1, le=1000)
    page_token: str | None = None
    person_fields: str | None = None


class SearchContactsInput(BaseModel):
    query: str
    page_size: int = Field(default=30, ge=1, le=1000)
    page_token: str | None = None
    person_fields: str | None = None


class GetContactInput(BaseModel):
    resource_name: str
    person_fields: str | None = None


def create_contacts_tools(
    user_id: str,
    connection_id: str,
) -> list[StructuredTool]:
    authenticated_user_id = UUID(str(user_id))
    selected_connection_id = UUID(str(connection_id))

    async def list_contacts(**arguments: Any) -> str:
        import json

        try:
            with SessionLocal() as db:
                service = ContactsService(db)
                result = await service.list_contacts(
                    user_id=authenticated_user_id,
                    connection_id=selected_connection_id,
                    page_size=arguments.get("page_size", 100),
                    page_token=arguments.get("page_token"),
                    person_fields=arguments.get("person_fields"),
                )
            return json.dumps({"ok": True, "data": result})
        except Exception as exc:
            return json.dumps({"ok": False, "error": {"message": str(exc)}})

    async def search_contacts(**arguments: Any) -> str:
        import json

        try:
            with SessionLocal() as db:
                service = ContactsService(db)
                result = await service.search_contacts(
                    user_id=authenticated_user_id,
                    connection_id=selected_connection_id,
                    query=arguments["query"],
                    page_size=arguments.get("page_size", 30),
                    page_token=arguments.get("page_token"),
                    person_fields=arguments.get("person_fields"),
                )
            return json.dumps({"ok": True, "data": result})
        except Exception as exc:
            return json.dumps({"ok": False, "error": {"message": str(exc)}})

    async def get_contact(**arguments: Any) -> str:
        import json

        try:
            with SessionLocal() as db:
                service = ContactsService(db)
                result = await service.get_contact(
                    user_id=authenticated_user_id,
                    connection_id=selected_connection_id,
                    resource_name=arguments["resource_name"],
                    person_fields=arguments.get("person_fields"),
                )
            return json.dumps({"ok": True, "data": result})
        except Exception as exc:
            return json.dumps({"ok": False, "error": {"message": str(exc)}})

    tools = [
        StructuredTool.from_function(
            coroutine=list_contacts,
            name="list_contacts",
            description="List Google Contacts for the authenticated user.",
            args_schema=ListContactsInput,
        ),
        StructuredTool.from_function(
            coroutine=search_contacts,
            name="search_contacts",
            description="Search Google Contacts by name, email or phone.",
            args_schema=SearchContactsInput,
        ),
        StructuredTool.from_function(
            coroutine=get_contact,
            name="get_contact",
            description="Get one Google Contact by resourceName.",
            args_schema=GetContactInput,
        ),
    ]
    return tools