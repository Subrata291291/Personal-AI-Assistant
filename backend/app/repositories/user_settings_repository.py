import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models.user_settings import UserSettings


class UserSettingsRepository:
    """
    Database repository for user-specific application settings.

    Every operation explicitly scopes records by user_id.
    This is mandatory because the backend DB role bypasses RLS.
    """

    def __init__(self, session: Session):
        self.session = session

    def get_settings(
        self,
        user_id: uuid.UUID,
    ) -> UserSettings | None:

        statement = select(UserSettings).where(
            UserSettings.user_id == user_id,
        )

        return self.session.scalar(statement)

    def get_or_create_settings(
        self,
        user_id: uuid.UUID,
    ) -> UserSettings:

        # Insert only if the user does not already have settings.
        statement = (
            insert(UserSettings)
            .values(
                user_id=user_id,
                memory_enabled=True,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id"],
            )
        )

        self.session.execute(statement)

        # Retrieve the existing or newly created settings.
        settings = self.get_settings(user_id)

        if settings is None:
            raise RuntimeError(
                "Unable to retrieve or create user settings."
            )

        return settings

    def update_memory_enabled(
        self,
        user_id: uuid.UUID,
        memory_enabled: bool,
    ) -> UserSettings:

        # Ensure the settings record exists first.
        self.get_or_create_settings(user_id)

        statement = (
            update(UserSettings)
            .where(
                UserSettings.user_id == user_id,
            )
            .values(
                memory_enabled=memory_enabled,
            )
            .returning(UserSettings)
        )

        settings = self.session.scalar(statement)

        if settings is None:
            raise RuntimeError(
                "Unable to update user memory settings."
            )

        return settings