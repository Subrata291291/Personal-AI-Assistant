from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models.user_connection import UserConnection


class UserConnectionRepository:
    """
    Database operations for external account connections.

    Every operation is explicitly scoped by user_id.
    Transaction management belongs to the service layer.
    """

    def __init__(self, db: Session):
        self.db = db

    def get_by_provider_account(
        self,
        user_id: UUID,
        provider: str,
        provider_account_id: str,
    ) -> UserConnection | None:
        statement = select(UserConnection).where(
            UserConnection.user_id == user_id,
            UserConnection.provider == provider.strip().lower(),
            UserConnection.provider_account_id == provider_account_id,
        )

        return self.db.scalar(statement)

    def create(
        self,
        user_id: UUID,
        provider: str,
        provider_account_id: str,
        email: str | None = None,
    ) -> UserConnection:
        connection = UserConnection(
            user_id=user_id,
            provider=provider.strip().lower(),
            provider_account_id=provider_account_id,
            email=email,
            status="connected",
            connected_at=datetime.now(timezone.utc),
        )

        self.db.add(connection)
        self.db.flush()

        return connection

    def update_connection(
        self,
        connection: UserConnection,
        email: str | None = None,
    ) -> UserConnection:
        connection.email = email
        connection.status = "connected"
        connection.connected_at = datetime.now(timezone.utc)

        self.db.flush()

        return connection

    def get_by_id(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> UserConnection | None:
        statement = select(UserConnection).where(
            UserConnection.id == connection_id,
            UserConnection.user_id == user_id,
        )

        return self.db.scalar(statement)


    def get_active_google_connection(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> UserConnection | None:

        statement = select(UserConnection).where(
            UserConnection.id == connection_id,
            UserConnection.user_id == user_id,
            UserConnection.provider == "google",
            UserConnection.status == "connected",
        )

        return self.db.scalar(statement)


    def list_by_user(
        self,
        user_id: UUID,
    ) -> list[UserConnection]:

        statement = (
            select(UserConnection)
            .where(UserConnection.user_id == user_id)
            .order_by(UserConnection.created_at.desc())
        )

        return list(self.db.scalars(statement).all())

    def delete_by_id(
        self,
        user_id: UUID,
        connection_id: UUID,
    ) -> bool:

        connection = self.get_by_id(
            user_id=user_id,
            connection_id=connection_id,
        )

        if connection is None:
            return False

        self.db.delete(connection)
        self.db.flush()

        return True