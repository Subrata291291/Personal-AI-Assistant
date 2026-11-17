from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models.drive_selected_file import DriveSelectedFile


class DriveSelectedFileRepository:
    """Database operations for the per-user Drive selected-file allowlist.

    Every operation is scoped by user_id. Contract content is never stored or
    returned by this repository; only minimal file metadata is kept.
    """

    def __init__(self, db: Session):
        self.db = db

    def list_by_user(
        self,
        user_id: UUID,
    ) -> list[DriveSelectedFile]:
        statement = (
            select(DriveSelectedFile)
            .where(DriveSelectedFile.user_id == user_id)
            .order_by(
                DriveSelectedFile.selected_at.desc(),
                DriveSelectedFile.id.desc(),
            )
        )
        return list(self.db.scalars(statement).all())

    def list_by_user_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> list[DriveSelectedFile]:
        statement = (
            select(DriveSelectedFile)
            .where(
                DriveSelectedFile.user_id == user_id,
                DriveSelectedFile.connection_id == connection_id,
            )
            .order_by(
                DriveSelectedFile.selected_at.desc(),
                DriveSelectedFile.id.desc(),
            )
        )
        return list(self.db.scalars(statement).all())

    def get_by_user_and_file(
        self,
        user_id: UUID,
        provider_file_id: str,
    ) -> DriveSelectedFile | None:
        statement = select(DriveSelectedFile).where(
            DriveSelectedFile.user_id == user_id,
            DriveSelectedFile.provider_file_id == provider_file_id,
        )
        return self.db.scalar(statement)

    def upsert(
        self,
        *,
        user_id: UUID,
        connection_id: UUID,
        provider_file_id: str,
        name: str,
        mime_type: str,
        modified_time=None,
        size_bytes: int | None = None,
    ) -> DriveSelectedFile:
        selected_file = self.get_by_user_and_file(
            user_id=user_id,
            provider_file_id=provider_file_id,
        )

        if selected_file is None:
            selected_file = DriveSelectedFile(
                user_id=user_id,
                connection_id=connection_id,
                provider_file_id=provider_file_id,
                name=name,
                mime_type=mime_type,
                modified_time=modified_time,
                size_bytes=size_bytes,
            )
            self.db.add(selected_file)
        else:
            selected_file.connection_id = connection_id
            selected_file.name = name
            selected_file.mime_type = mime_type
            selected_file.modified_time = modified_time
            selected_file.size_bytes = size_bytes

        self.db.flush()
        return selected_file

    def delete_by_user_and_file(
        self,
        user_id: UUID,
        provider_file_id: str,
    ) -> bool:
        result = self.db.execute(
            delete(DriveSelectedFile).where(
                DriveSelectedFile.user_id == user_id,
                DriveSelectedFile.provider_file_id == provider_file_id,
            )
        )
        self.db.flush()
        return bool(result.rowcount)
