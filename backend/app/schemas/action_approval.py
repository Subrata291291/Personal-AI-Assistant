import json
import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from app.schemas.email_draft import normalize_email_address
from app.schemas.calendar_approval import (
    CalendarCreateEventParameters,
    CalendarDeleteEventParameters,
    CalendarUpdateEventParameters,
)


ActionType = Literal[
    "gmail.send",
    "gmail.send_draft",
    "gmail.reply",
    "gmail.reply_all",
    "gmail.forward",
    "gmail.archive",
    "gmail.trash",
    "gmail.save_draft",
    "drive.create_file",
    "drive.update_file",
    "drive.delete_file",
    "calendar.create_event",
    "calendar.update_event",
    "calendar.delete_event",
    "contacts.create_contact",
    "contacts.update_contact",
    "contacts.delete_contact",
]

ApprovalStatus = Literal[
    "pending",
    "approved",
    "executing",
    "completed",
    "rejected",
    "expired",
    "failed",
    "execution_uncertain",
]

ACTION_PARAMETER_FIELDS: dict[str, tuple[set[str], set[str]]] = {
    "gmail.send": (
        {"to", "subject", "body"},
        {"cc", "bcc"},
    ),
    "gmail.send_draft": (
        {"draft_id", "message_id", "to", "subject", "body"},
        {"cc", "bcc"},
    ),
    "gmail.reply": (
        {"message_id", "to", "subject", "body"},
        {"cc"},
    ),
    "gmail.reply_all": (
        {"message_id", "to", "subject", "body"},
        {"cc"},
    ),
    "gmail.forward": (
        {"message_id", "to", "subject", "body"},
        {"cc", "bcc"},
    ),
    "gmail.archive": ({"message_id"}, set()),
    "gmail.trash": ({"message_id"}, set()),
    "gmail.save_draft": (
        {"subject", "body"},
        {"to", "cc", "bcc", "message_id", "thread_id"},
    ),
    "drive.create_file": (
        {"name", "mime_type", "content"},
        set(),
    ),
    "drive.update_file": (
        {"file_id"},
        {"name", "mime_type", "content"},
    ),
    "drive.delete_file": ({"file_id"}, set()),
    "calendar.create_event": (
        {"calendar_id", "summary", "start", "end", "all_day"},
        {"description", "time_zone", "location", "attendees", "reminders"},
    ),
    "calendar.update_event": (
        {
            "calendar_id",
            "event_id",
            "expected_etag",
            "current_event",
            "changes",
        },
        set(),
    ),
    "calendar.delete_event": (
        {"calendar_id", "event_id", "expected_etag", "current_event"},
        set(),
    ),
    "contacts.create_contact": (
        {"display_name"},
        {"email", "phone"},
    ),
    "contacts.update_contact": (
        {"contact_id"},
        {"display_name", "email", "phone"},
    ),
    "contacts.delete_contact": ({"contact_id"}, set()),
}

_RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,512}$")
_CALENDAR_ID_PATTERN = re.compile(r"^[A-Za-z0-9._@+-]{1,1024}$")
_SAFE_RESULT_DENIED_KEYS = re.compile(
    r"(?:access|refresh|client)?_?(?:token|secret|credential|authorization|"
    r"password|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)


class ApprovalProposalCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    conversation_id: UUID = Field(strict=False)
    source_message_id: UUID = Field(strict=False)
    connection_id: UUID = Field(strict=False)
    action_type: ActionType
    parameters: dict[str, JsonValue] = Field(min_length=1, max_length=30)

    @field_validator("parameters")
    @classmethod
    def validate_parameter_size(
        cls,
        value: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if len(encoded.encode("utf-8")) > 32_000:
            raise ValueError("Approval parameters are too large.")
        return value

    @model_validator(mode="after")
    def validate_action_parameters(self) -> "ApprovalProposalCreate":
        if self.action_type == "calendar.create_event":
            validated = CalendarCreateEventParameters.model_validate(
                self.parameters
            )
            self.parameters = validated.model_dump(mode="json")
            return self
        if self.action_type == "calendar.update_event":
            validated = CalendarUpdateEventParameters.model_validate(
                self.parameters
            )
            self.parameters = validated.model_dump(mode="json")
            return self
        if self.action_type == "calendar.delete_event":
            validated = CalendarDeleteEventParameters.model_validate(
                self.parameters
            )
            self.parameters = validated.model_dump(mode="json")
            return self
        required, optional = ACTION_PARAMETER_FIELDS[self.action_type]
        supplied = set(self.parameters)
        if not required.issubset(supplied):
            missing = ", ".join(sorted(required - supplied))
            raise ValueError(f"Missing required action parameters: {missing}.")
        if supplied - required - optional:
            unknown = ", ".join(sorted(supplied - required - optional))
            raise ValueError(f"Unsupported action parameters: {unknown}.")

        for key, value in self.parameters.items():
            if key in {"to", "cc", "bcc", "attendees"}:
                if (
                    not isinstance(value, list)
                    or len(value) > 50
                    or any(not isinstance(item, str) for item in value)
                ):
                    raise ValueError(f"{key} must be a list of strings.")
                if key in {"to", "cc", "bcc", "attendees"}:
                    normalized = [
                        normalize_email_address(item)
                        for item in value
                    ]
                    if any(item is None for item in normalized):
                        raise ValueError(
                            f"{key} contains an invalid email address."
                        )
                    self.parameters[key] = [
                        item for item in normalized if item is not None
                    ]
                continue

            if not isinstance(value, str):
                raise ValueError(f"{key} must be a string.")
            if len(value) > _parameter_limit(key):
                raise ValueError(f"{key} is too long.")
            if any(
                (ord(character) < 32 and character not in "\r\n\t")
                or ord(character) == 127
                for character in value
            ):
                raise ValueError(f"{key} contains unsupported control characters.")
            if key == "email":
                normalized_email = normalize_email_address(value)
                if normalized_email is None:
                    raise ValueError("email is not a valid email address.")
                self.parameters[key] = normalized_email

        for key in ("subject", "title", "name", "display_name"):
            value = self.parameters.get(key)
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"{key} cannot be empty.")
        if self.action_type in {
            "gmail.send",
            "gmail.send_draft",
            "gmail.reply",
            "gmail.reply_all",
            "gmail.forward",
            "gmail.save_draft",
        } and not str(self.parameters.get("body", "")).strip():
            raise ValueError("body cannot be empty.")

        for key in (
            "message_id",
            "thread_id",
            "file_id",
            "event_id",
            "contact_id",
        ):
            value = self.parameters.get(key)
            if isinstance(value, str) and not _RESOURCE_ID_PATTERN.fullmatch(value):
                raise ValueError(f"{key} is invalid.")
        calendar_id = self.parameters.get("calendar_id")
        if calendar_id is not None and (
            not isinstance(calendar_id, str)
            or calendar_id in {".", ".."}
            or not _CALENDAR_ID_PATTERN.fullmatch(calendar_id)
        ):
            raise ValueError("calendar_id is invalid.")
        draft_id = self.parameters.get("draft_id")
        if isinstance(draft_id, str) and not _RESOURCE_ID_PATTERN.fullmatch(draft_id):
            raise ValueError("draft_id is invalid.")

        for key in ("to", "cc", "bcc"):
            recipients = self.parameters.get(key)
            if isinstance(recipients, list):
                folded = [address.casefold() for address in recipients]
                if len(folded) != len(set(folded)):
                    raise ValueError("A recipient cannot appear more than once.")
        recipients = [
            address.casefold()
            for key in ("to", "cc", "bcc")
            for address in self.parameters.get(key, [])
            if isinstance(address, str)
        ]
        if len(recipients) != len(set(recipients)):
            raise ValueError("A recipient cannot appear more than once.")

        event_start = self.parameters.get("starts_at")
        event_end = self.parameters.get("ends_at")
        start = (
            _parse_aware_datetime(event_start, "starts_at")
            if event_start is not None
            else None
        )
        end = (
            _parse_aware_datetime(event_end, "ends_at")
            if event_end is not None
            else None
        )
        if start is not None and end is not None:
            if end <= start:
                raise ValueError("ends_at must be later than starts_at.")

        if self.action_type in {
            "gmail.send",
            "gmail.send_draft",
            "gmail.reply",
            "gmail.reply_all",
            "gmail.forward",
        } and not self.parameters.get("to"):
            raise ValueError("At least one To recipient is required.")

        if self.action_type == "drive.update_file" and not (
            set(self.parameters) - {"file_id"}
        ):
            raise ValueError("At least one file change is required.")
        if self.action_type == "contacts.update_contact" and not (
            set(self.parameters) - {"contact_id"}
        ):
            raise ValueError("At least one contact change is required.")
        if self.action_type == "contacts.create_contact" and not (
            self.parameters.get("email") or self.parameters.get("phone")
        ):
            raise ValueError("A new contact requires an email or phone number.")
        return self


class GmailDraftSendApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_id: UUID
    to: list[EmailStr] = Field(min_length=1, max_length=50)
    cc: list[EmailStr] = Field(default_factory=list, max_length=50)
    bcc: list[EmailStr] = Field(default_factory=list, max_length=50)
    subject: str = Field(max_length=500)
    body: str = Field(min_length=1, max_length=20_000)


class ActionApprovalProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    proposal_id: UUID
    message_id: UUID
    source_message_id: UUID
    user_id: UUID
    conversation_id: UUID
    action_type: ActionType
    parameters: dict[str, JsonValue]
    connection_id: UUID
    account_email: str | None = None
    created_at: datetime
    expires_at: datetime
    status: ApprovalStatus
    approved_at: datetime | None = None
    execution_started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, JsonValue] | None = None
    execution_available: bool | None = None
    integrity_schema_version: int | None = None
    payload_fingerprint: str | None = None
    operation_id: UUID | None = None
    calendar_event_id: str | None = None

    @model_validator(mode="after")
    def require_aware_timestamps(self) -> "ActionApprovalProposal":
        for value in (
            self.created_at,
            self.expires_at,
            self.approved_at,
            self.execution_started_at,
            self.completed_at,
        ):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Approval timestamps must include a timezone.")
        return self


class ActionApprovalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    proposal: ActionApprovalProposal


def _parameter_limit(key: str) -> int:
    if key in {"body", "content"}:
        return 20_000
    if key in {"subject", "title", "name", "display_name"}:
        return 500
    if key in {"email", "to", "cc", "bcc", "phone"}:
        return 320
    if key == "mime_type":
        return 255
    if key == "time_zone":
        return 128
    return 2_000


def _parse_aware_datetime(value: object, key: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be an ISO-8601 datetime string.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{key} must be an ISO-8601 datetime string.") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{key} must include a timezone.")
    return parsed


def validate_safe_result(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    if len(value) > 20:
        raise ValueError("Execution result contains too many fields.")

    def reject_credential_keys(item: JsonValue) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if _SAFE_RESULT_DENIED_KEYS.search(key):
                    raise ValueError(
                        "Execution results cannot contain credentials."
                    )
                reject_credential_keys(nested)
        elif isinstance(item, list):
            for nested in item:
                reject_credential_keys(nested)

    reject_credential_keys(value)
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) > 8_000:
        raise ValueError("Execution result is too large.")
    return value
