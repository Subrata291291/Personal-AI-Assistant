
import base64
import re
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Sequence


class GmailMessageBuilder:
    """Build Gmail API-compatible MIME messages."""

    MAX_RECIPIENTS = 100
    MAX_SUBJECT_LENGTH = 998
    MAX_BODY_LENGTH = 1_000_000

    @staticmethod
    def _validate_email(email: str) -> str:
        if not isinstance(email, str):
            raise ValueError("Email address must be a string.")

        candidate = email.strip()
        if "<" in candidate or ">" in candidate:
            if not re.fullmatch(r'\s*(.*?)<\s*([^<>]+)\s*>\s*', candidate):
                raise ValueError(f"Invalid email address: {email}")
            display_name, parsed_email = parseaddr(candidate)
        else:
            display_name, parsed_email = "", candidate

        if (
            not parsed_email
            or "@" not in parsed_email
            or any(character.isspace() for character in parsed_email)
            or "\r" in candidate
            or "\n" in candidate
            or any(ord(character) < 32 or ord(character) == 127 for character in display_name)
        ):
            raise ValueError(f"Invalid email address: {email}")

        return formataddr((display_name, parsed_email)) if display_name else parsed_email

    @classmethod
    def _validate_recipients(
        cls,
        recipients: Sequence[str],
        required: bool = False,
    ) -> list[str]:

        if isinstance(recipients, str):
            raise ValueError(
                "Recipients must be a list of email addresses."
            )

        validated = [
            cls._validate_email(item)
            for item in recipients
        ]

        if required and not validated:
            raise ValueError(
                "At least one recipient is required."
            )

        if len(validated) > cls.MAX_RECIPIENTS:
            raise ValueError("Too many recipients.")

        return validated

    @staticmethod
    def _validate_message_header(
        value: str | None,
        field_name: str,
    ) -> str | None:

        if value is None:
            return None

        if not isinstance(value, str):
            raise ValueError(
                f"{field_name} must be a string."
            )

        value = value.strip()

        if "\r" in value or "\n" in value:
            raise ValueError(
                f"Invalid {field_name} header."
            )

        if len(value) > 998:
            raise ValueError(
                f"{field_name} header is too long."
            )

        return value or None

    @classmethod
    def build(
        cls,
        to: Sequence[str],
        subject: str,
        body: str,
        cc: Sequence[str] | None = None,
        bcc: Sequence[str] | None = None,
        html_body: str | None = None,
        reply_to: str | None = None,
        in_reply_to: str | None = None,
        references: str | None = None,
        require_recipient: bool = True,
        draft_operation_id: str | None = None,
    ) -> str:

        to_list = cls._validate_recipients(
            to,
            required=require_recipient,
        )

        cc_list = cls._validate_recipients(cc or [])
        bcc_list = cls._validate_recipients(bcc or [])

        if not isinstance(subject, str):
            raise ValueError("Subject must be a string.")

        if len(subject) > cls.MAX_SUBJECT_LENGTH:
            raise ValueError("Subject is too long.")

        if "\r" in subject or "\n" in subject:
            raise ValueError("Invalid email subject.")

        if not isinstance(body, str):
            raise ValueError("Email body must be a string.")

        if len(body) > cls.MAX_BODY_LENGTH:
            raise ValueError("Email body is too large.")

        if html_body is not None:
            if not isinstance(html_body, str):
                raise ValueError("HTML body must be a string.")

            if len(html_body) > cls.MAX_BODY_LENGTH:
                raise ValueError("HTML body is too large.")

        in_reply_to = cls._validate_message_header(
            in_reply_to,
            "In-Reply-To",
        )

        references = cls._validate_message_header(
            references,
            "References",
        )

        message = EmailMessage()

        if to_list:
            message["To"] = ", ".join(to_list)
        message["Subject"] = subject

        if cc_list:
            message["Cc"] = ", ".join(cc_list)

        if bcc_list:
            message["Bcc"] = ", ".join(bcc_list)

        if reply_to:
            message["Reply-To"] = cls._validate_email(
                reply_to
            )

        if in_reply_to:
            message["In-Reply-To"] = in_reply_to

        if references:
            message["References"] = references
        if draft_operation_id:
            message["X-Personal-AI-Draft-Operation-ID"] = (
                cls._validate_message_header(
                    draft_operation_id,
                    "Draft operation ID",
                )
            )

        message.set_content(body)

        if html_body:
            message.add_alternative(
                html_body,
                subtype="html",
            )

        raw_message = base64.urlsafe_b64encode(
            message.as_bytes()
        ).decode("utf-8").rstrip("=")

        return raw_message
