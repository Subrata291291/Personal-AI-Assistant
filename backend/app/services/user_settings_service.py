import uuid

from sqlalchemy.orm import Session

from app.db.models.user_settings import UserSettings
from app.repositories.user_settings_repository import (
    UserSettingsRepository,
)


class UserSettingsService:
    """
    Business logic for user-specific application settings.
    """

    def __init__(self, session: Session):
        self.session = session
        self.repository = UserSettingsRepository(session)

    def get_settings(
        self,
        user_id: uuid.UUID,
    ) -> UserSettings:

        try:
            settings = self.repository.get_or_create_settings(
                user_id=user_id,
            )

            self.session.commit()
            self.session.refresh(settings)

            return settings

        except Exception:
            self.session.rollback()
            raise

    def update_memory_enabled(
        self,
        user_id: uuid.UUID,
        memory_enabled: bool,
    ) -> UserSettings:

        if not isinstance(memory_enabled, bool):
            raise ValueError(
                "memory_enabled must be a boolean."
            )

        try:
            settings = self.repository.update_memory_enabled(
                user_id=user_id,
                memory_enabled=memory_enabled,
            )

            self.session.commit()
            self.session.refresh(settings)

            return settings

        except Exception:
            self.session.rollback()
            raise