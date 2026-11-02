
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db.models.oauth_state import OAuthState


class OAuthStateRepository:
    """
    Database operations for OAuth state management.

    Transaction commit and rollback are managed by the service layer.
    """

    def __init__(self, db: Session):
        self.db = db

    def create(
        self,
        user_id: UUID,
        provider: str,
        state_hash: str,
        code_verifier_encrypted: str,
        redirect_uri: str,
        expires_at: datetime,
    ) -> OAuthState:
        """
        Create a new OAuth state record.

        The original state must never be stored here.
        Only its SHA-256 hash is accepted.
        """

        oauth_state = OAuthState(
            user_id=user_id,
            provider=provider.strip().lower(),
            state_hash=state_hash,
            code_verifier_encrypted=code_verifier_encrypted,
            redirect_uri=redirect_uri,
            expires_at=expires_at,
        )

        self.db.add(oauth_state)
        self.db.flush()

        return oauth_state

    def consume_state(
        self,
        user_id: UUID,
        provider: str,
        state_hash: str,
        now: datetime,
    ) -> OAuthState | None:
        """
        Atomically consume an unexpired, unused OAuth state.

        Returns the consumed record only once.

        Returns None when:
        - State does not exist.
        - User does not own the state.
        - Provider does not match.
        - State has expired.
        - State has already been consumed.
        """

        statement = (
            update(OAuthState)
            .where(
                OAuthState.user_id == user_id,
                OAuthState.provider == provider.strip().lower(),
                OAuthState.state_hash == state_hash,
                OAuthState.expires_at > now,
                OAuthState.consumed_at.is_(None),
            )
            .values(consumed_at=now)
            .returning(OAuthState)
            .execution_options(synchronize_session=False)
        )

        result = self.db.execute(statement)

        return result.scalar_one_or_none()

    def get_by_state_hash(
        self,
        user_id: UUID,
        provider: str,
        state_hash: str,
    ) -> OAuthState | None:
        """
        Retrieve an OAuth state without consuming it.

        Intended for controlled internal inspection.
        """

        statement = select(OAuthState).where(
            OAuthState.user_id == user_id,
            OAuthState.provider == provider.strip().lower(),
            OAuthState.state_hash == state_hash,
        )

        return self.db.scalar(statement)

    def delete_expired(
        self,
        now: datetime,
    ) -> int:
        """
        Delete expired OAuth state records.

        This is intended for periodic backend cleanup.
        """

        from sqlalchemy import delete

        statement = (
            delete(OAuthState)
            .where(OAuthState.expires_at <= now)
        )

        result = self.db.execute(statement)

        return result.rowcount or 0
