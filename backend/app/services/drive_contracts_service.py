import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models.drive_selected_file import DriveSelectedFile
from app.db.session import SessionLocal
from app.integrations.google import scopes as google_scopes
from app.integrations.google.api_client import GoogleAPIClient
from app.integrations.google.exceptions import GoogleAPIError
from app.repositories.drive_selected_file_repository import (
    DriveSelectedFileRepository,
)
from app.repositories.oauth_credential_repository import (
    OAuthCredentialRepository,
)
from app.repositories.user_connection_repository import (
    UserConnectionRepository,
)
from app.services.drive_text_extraction import (
    DOCX_MIME_TYPE,
    GOOGLE_DOC_MIME_TYPE,
    PDF_MIME_TYPE,
    SUPPORTED_CONTRACT_MIME_TYPES,
    ExtractedDocument,
    extract_docx_text,
    extract_google_doc_text,
    extract_pdf_text,
)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
CONTRACT_MIME_TYPES = tuple(sorted(SUPPORTED_CONTRACT_MIME_TYPES))
FILE_METADATA_FIELDS = "id,name,mimeType,modifiedTime,size,trashed"
FILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,200}$")


class DriveContractsError(ValueError):
    """Base error with a safe, user-facing message."""


class DriveConnectionMissingError(DriveContractsError):
    pass


class DriveConnectionSelectionRequiredError(DriveContractsError):
    pass


class DriveScopeMissingError(DriveContractsError):
    pass


class DriveFileNotSelectedError(DriveContractsError):
    pass


class DriveFileInaccessibleError(DriveContractsError):
    pass


class DriveUnsupportedFileError(DriveContractsError):
    pass


@dataclass(frozen=True)
class SelectedContract:
    provider_file_id: str
    name: str
    mime_type: str
    modified_time: datetime | None
    size_bytes: int | None

    @property
    def extraction_kind(self) -> str:
        if self.mime_type == PDF_MIME_TYPE:
            return "pdf"
        if self.mime_type == DOCX_MIME_TYPE:
            return "docx"
        return "google_doc"


@dataclass(frozen=True)
class ExtractedContract:
    contract: SelectedContract
    document: ExtractedDocument


class DriveContractsService:
    """Read-only operations over a user's explicitly selected Drive files."""

    def __init__(
        self,
        db: Session,
        api_client: GoogleAPIClient | None = None,
    ):
        self.db = db
        self.api_client = api_client or GoogleAPIClient()
        self.selected_file_repository = DriveSelectedFileRepository(db)
        self.connection_repository = UserConnectionRepository(db)
        self.credential_repository = OAuthCredentialRepository(db)

    @staticmethod
    def normalize_file_id(file_id: str) -> str:
        if not isinstance(file_id, str) or not FILE_ID_PATTERN.fullmatch(
            file_id.strip()
        ):
            raise DriveFileNotSelectedError(
                "The requested Drive file ID is not valid."
            )
        return file_id.strip()

    def _validate_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> None:
        connection = self.connection_repository.get_active_google_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        if connection is None:
            raise DriveConnectionMissingError(
                "The connected Google account is no longer available."
            )

        credential = self.credential_repository.get_by_connection(
            user_id=user_id,
            connection_id=connection_id,
        )
        granted_scopes = set(credential.scopes or []) if credential else set()
        if not granted_scopes & google_scopes.DRIVE_CONTRACTS_READ_SCOPES:
            raise DriveScopeMissingError(
                "Google Drive read permission has not been granted for the "
                "selected account."
            )

    async def _google_get(
        self,
        user_id: UUID,
        connection_id: UUID,
        file_id: str,
    ) -> dict[str, Any]:
        api_db = SessionLocal()
        try:
            result = await self.api_client.request(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                method="GET",
                url=f"{DRIVE_API_BASE}/files/{quote(file_id, safe='')}",
                params={
                    "fields": FILE_METADATA_FIELDS,
                    "supportsAllDrives": "false",
                },
            )
            return result if isinstance(result, dict) else {}
        finally:
            api_db.close()

    async def _download(
        self,
        user_id: UUID,
        connection_id: UUID,
        file_id: str,
        *,
        export_google_doc: bool,
    ) -> bytes:
        api_db = SessionLocal()
        try:
            if export_google_doc:
                url = (
                    f"{DRIVE_API_BASE}/files/"
                    f"{quote(file_id, safe='')}/export"
                )
                params = {"mimeType": "text/plain"}
            else:
                url = f"{DRIVE_API_BASE}/files/{quote(file_id, safe='')}"
                params = {"alt": "media", "supportsAllDrives": "false"}

            content, _content_type = await self.api_client.download_bytes(
                db=api_db,
                user_id=str(user_id),
                connection_id=str(connection_id),
                url=url,
                params=params,
                max_bytes=settings.DRIVE_CONTRACTS_MAX_DOWNLOAD_BYTES,
            )
            return content
        finally:
            api_db.close()

    @staticmethod
    def _contract_from_metadata(
        metadata: dict[str, Any],
        *,
        expected_file_id: str,
    ) -> SelectedContract:
        provider_file_id = metadata.get("id")
        name = metadata.get("name")
        mime_type = metadata.get("mimeType")

        if provider_file_id != expected_file_id:
            raise DriveFileInaccessibleError(
                "The selected Drive file could not be verified."
            )
        if metadata.get("trashed") is True:
            raise DriveFileInaccessibleError(
                "The selected Drive file is in the trash."
            )
        if not isinstance(name, str) or not name.strip():
            raise DriveFileInaccessibleError(
                "The selected Drive file has no usable name."
            )
        if mime_type not in SUPPORTED_CONTRACT_MIME_TYPES:
            raise DriveUnsupportedFileError(
                "Only PDF, DOCX, and Google Docs contracts are supported."
            )

        modified_time = None
        raw_modified_time = metadata.get("modifiedTime")
        if isinstance(raw_modified_time, str):
            try:
                modified_time = datetime.fromisoformat(
                    raw_modified_time.replace("Z", "+00:00")
                )
            except ValueError:
                modified_time = None
        if (
            modified_time is not None
            and modified_time.tzinfo is None
        ):
            modified_time = modified_time.replace(tzinfo=timezone.utc)

        raw_size = metadata.get("size")
        try:
            size_bytes = int(raw_size) if raw_size is not None else None
        except (TypeError, ValueError):
            size_bytes = None
        if size_bytes is not None and size_bytes < 0:
            size_bytes = None

        return SelectedContract(
            provider_file_id=provider_file_id,
            name=name.strip()[:512],
            mime_type=mime_type,
            modified_time=modified_time,
            size_bytes=size_bytes,
        )

    async def _fetch_live_contract(
        self,
        user_id: UUID,
        connection_id: UUID,
        file_id: str,
    ) -> SelectedContract:
        try:
            metadata = await self._google_get(
                user_id=user_id,
                connection_id=connection_id,
                file_id=file_id,
            )
        except GoogleAPIError as exc:
            if exc.status_code in {401, 403, 404}:
                raise DriveFileInaccessibleError(
                    "The selected Drive file is no longer accessible."
                ) from exc
            raise DriveContractsError(
                "Google Drive could not be reached. Please try again."
            ) from exc

        return self._contract_from_metadata(
            metadata,
            expected_file_id=file_id,
        )

    async def register_selected_file(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        file_id: str,
    ) -> DriveSelectedFile:
        normalized_file_id = self.normalize_file_id(file_id)
        self._validate_connection(user_id, connection_id)

        contract = await self._fetch_live_contract(
            user_id=user_id,
            connection_id=connection_id,
            file_id=normalized_file_id,
        )

        selected_file = self.selected_file_repository.upsert(
            user_id=user_id,
            connection_id=connection_id,
            provider_file_id=contract.provider_file_id,
            name=contract.name,
            mime_type=contract.mime_type,
            modified_time=contract.modified_time,
            size_bytes=contract.size_bytes,
        )
        self.db.commit()
        return selected_file

    def list_selected_files(
        self,
        *,
        user_id: UUID,
        connection_id: UUID | None = None,
    ) -> list[DriveSelectedFile]:
        if connection_id is None:
            return self.selected_file_repository.list_by_user(user_id)
        return self.selected_file_repository.list_by_user_connection(
            user_id=user_id,
            connection_id=connection_id,
        )

    def remove_selected_file(
        self,
        *,
        user_id: UUID,
        file_id: str,
    ) -> bool:
        """Remove a file from the app allowlist.

        This only deletes stored selection metadata. It never deletes, edits,
        or changes permissions on the Google Drive file itself.
        """
        normalized_file_id = self.normalize_file_id(file_id)
        removed = self.selected_file_repository.delete_by_user_and_file(
            user_id=user_id,
            provider_file_id=normalized_file_id,
        )
        self.db.commit()
        return removed

    def get_allowlisted_file(
        self,
        *,
        user_id: UUID,
        file_id: str,
    ) -> DriveSelectedFile:
        normalized_file_id = self.normalize_file_id(file_id)
        selected_file = self.selected_file_repository.get_by_user_and_file(
            user_id=user_id,
            provider_file_id=normalized_file_id,
        )
        if selected_file is None:
            raise DriveFileNotSelectedError(
                "That Drive file has not been selected for this account."
            )
        return selected_file

    async def get_selected_file_details(
        self,
        *,
        user_id: UUID,
        file_id: str,
    ) -> tuple[DriveSelectedFile, SelectedContract]:
        selected_file = self.get_allowlisted_file(
            user_id=user_id,
            file_id=file_id,
        )
        self._validate_connection(user_id, selected_file.connection_id)
        contract = await self._fetch_live_contract(
            user_id=user_id,
            connection_id=selected_file.connection_id,
            file_id=selected_file.provider_file_id,
        )
        return selected_file, contract

    async def get_selected_file_metadata(
        self,
        *,
        user_id: UUID,
        file_id: str,
    ) -> SelectedContract:
        _selected_file, contract = await self.get_selected_file_details(
            user_id=user_id,
            file_id=file_id,
        )
        return contract

    async def extract_selected_file_text(
        self,
        *,
        user_id: UUID,
        file_id: str,
        max_chars: int | None = None,
    ) -> ExtractedContract:
        selected_file = self.get_allowlisted_file(
            user_id=user_id,
            file_id=file_id,
        )
        self._validate_connection(user_id, selected_file.connection_id)

        contract = await self._fetch_live_contract(
            user_id=user_id,
            connection_id=selected_file.connection_id,
            file_id=selected_file.provider_file_id,
        )

        limit = (
            max_chars
            if max_chars is not None
            else settings.DRIVE_CONTRACTS_MAX_CHARS_PER_DOCUMENT
        )

        content = await self._download(
            user_id=user_id,
            connection_id=selected_file.connection_id,
            file_id=selected_file.provider_file_id,
            export_google_doc=contract.mime_type == GOOGLE_DOC_MIME_TYPE,
        )

        try:
            if contract.mime_type == PDF_MIME_TYPE:
                document = extract_pdf_text(content, max_chars=limit)
            elif contract.mime_type == DOCX_MIME_TYPE:
                document = extract_docx_text(content, max_chars=limit)
            else:
                document = extract_google_doc_text(content, max_chars=limit)
        except ValueError as exc:
            raise DriveContractsError(str(exc)) from exc

        return ExtractedContract(contract=contract, document=document)
