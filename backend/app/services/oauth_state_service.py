
import base64
import hashlib
import secrets

from datetime import datetime, timedelta, timezone
from uuid import UUID
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import encrypt_data, decrypt_data
from app.repositories.oauth_state_repository import OAuthStateRepository


class OAuthStateService:
    """
    Manages secure OAuth state generation and validation.

    Security features:
    - Cryptographically secure random state
    - SHA-256 state hashing
    - PKCE S256 challenge generation
    - AES-256-GCM verifier encryption
    - State expiration
    - Atomic one-time state consumption
    """

    STATE_EXPIRY_MINUTES = 10

    def __init__(self, db: Session):
        self.db = db
        self.repository = OAuthStateRepository(db)

    @staticmethod
    def _generate_state() -> str:
        """Generate a cryptographically secure OAuth state."""
        return secrets.token_urlsafe(32)

    @staticmethod
    def _hash_state(state: str) -> str:
        """Return SHA-256 hex digest of the original state."""
        return hashlib.sha256(
            state.encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _generate_code_verifier() -> str:
        """Generate a PKCE code verifier."""
        return secrets.token_urlsafe(64)

    @staticmethod
    def _generate_code_challenge(code_verifier: str) -> str:
        """Generate an S256 PKCE challenge."""
        digest = hashlib.sha256(
            code_verifier.encode("ascii")
        ).digest()

        return base64.urlsafe_b64encode(
            digest
        ).rstrip(b"=").decode("ascii")

    def create_state(
        self,
        user_id: UUID,
        provider: str,
        redirect_uri: str,
    ) -> dict[str, Any]:
        """
        Generate OAuth state and PKCE values,
        then persist their protected representation.
        """

        normalized_provider = provider.strip().lower()

        if not normalized_provider:
            raise ValueError("OAuth provider cannot be empty.")

        if not redirect_uri or not redirect_uri.strip():
            raise ValueError("Redirect URI cannot be empty.")

        original_state = self._generate_state()
        state_hash = self._hash_state(original_state)

        code_verifier = self._generate_code_verifier()
        encrypted_verifier = encrypt_data(code_verifier)

        code_challenge = self._generate_code_challenge(
            code_verifier
        )

        now = datetime.now(timezone.utc)

        expires_at = now + timedelta(
            minutes=self.STATE_EXPIRY_MINUTES
        )

        try:
            self.repository.create(
                user_id=user_id,
                provider=normalized_provider,
                state_hash=state_hash,
                code_verifier_encrypted=encrypted_verifier,
                redirect_uri=redirect_uri.strip(),
                expires_at=expires_at,
            )

            self.db.commit()

        except Exception:
            self.db.rollback()
            raise

        return {
            "state": original_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "expires_at": expires_at,
        }

    def consume_state(
        self,
        provider: str,
        state: str,
    ) -> dict[str, Any] | None:
        """
        Atomically consume an OAuth state and recover
        its PKCE verifier and initiating user.

        Invalid, expired or previously consumed states
        return None.
        """

        if not state or not state.strip():
            return None

        normalized_provider = provider.strip().lower()

        if not normalized_provider:
            return None

        state_hash = self._hash_state(state)
        now = datetime.now(timezone.utc)

        try:
            oauth_state = self.repository.consume_state(
                provider=normalized_provider,
                state_hash=state_hash,
                now=now,
            )

            if oauth_state is None:
                self.db.rollback()
                return None

            encrypted_verifier = oauth_state.code_verifier_encrypted
            stored_user_id = oauth_state.user_id
            stored_provider = oauth_state.provider
            stored_redirect_uri = oauth_state.redirect_uri
            consumed_at = oauth_state.consumed_at
            stored_state_hash = oauth_state.state_hash

            # Commit before decryption to prevent state replay.
            self.db.commit()

        except Exception:
            self.db.rollback()
            raise

        code_verifier = decrypt_data(encrypted_verifier)

        return {
            "user_id": stored_user_id,
            "provider": stored_provider,
            "code_verifier": code_verifier,
            "redirect_uri": stored_redirect_uri,
            "consumed_at": consumed_at,
            "state_hash": stored_state_hash,
        }

    def lock_consumed_state_for_callback(
        self,
        user_id: UUID,
        provider: str,
        state: str,
    ) -> bool:
        """Lock a consumed state while the callback creates its connection."""
        if not state or not state.strip():
            return False
        oauth_state = self.repository.get_consumed_state_for_update(
            user_id=user_id,
            provider=provider,
            state_hash=self._hash_state(state),
        )
        return oauth_state is not None

    def invalidate_user_states(
        self,
        user_id: UUID,
        provider: str,
    ) -> int:
        """Invalidate outstanding and consumed OAuth flows for an account."""
        return self.repository.delete_by_user_provider(
            user_id=user_id,
            provider=provider,
        )


    def delete_expired_states(self) -> int:
        """Delete expired OAuth states."""

        now = datetime.now(timezone.utc)

        try:
            deleted_count = self.repository.delete_expired(
                now=now
            )

            self.db.commit()

            return deleted_count

        except Exception:
            self.db.rollback()
            raise
