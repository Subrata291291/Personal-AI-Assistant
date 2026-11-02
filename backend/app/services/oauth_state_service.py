
import base64
import hashlib
import secrets

from datetime import datetime, timedelta, timezone
from uuid import UUID
from typing import Any

from app.core.security import encrypt_data, decrypt_data
from app.repositories.oauth_state_repository import OAuthStateRepository


class OAuthStateService:
    """
    Manages secure OAuth state generation and validation.

    Uses:
    - Cryptographically secure random state
    - SHA-256 state hashing
    - PKCE S256 challenge generation
    - AES-256-GCM verifier encryption
    - Expiration and one-time consumption
    """

    STATE_EXPIRY_MINUTES = 10

    def __init__(self, db):
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
        """
        Generate a PKCE code verifier.

        token_urlsafe(64) produces a verifier within
        the RFC 7636 required 43-128 character range.
        """
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

        self.repository.create(
            user_id=user_id,
            provider=normalized_provider,
            state_hash=state_hash,
            code_verifier_encrypted=encrypted_verifier,
            redirect_uri=redirect_uri.strip(),
            expires_at=expires_at,
        )

        return {
            "state": original_state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "expires_at": expires_at,
        }

    def consume_state(
        self,
        user_id: UUID,
        provider: str,
        state: str,
    ) -> dict[str, Any] | None:
        """
        Atomically consume an OAuth state and recover
        its encrypted PKCE verifier.

        Returns None for invalid, expired or already-used state.
        """

        if not state or not state.strip():
            return None

        normalized_provider = provider.strip().lower()
        state_hash = self._hash_state(state)

        now = datetime.now(timezone.utc)

        oauth_state = self.repository.consume_state(
            user_id=user_id,
            provider=normalized_provider,
            state_hash=state_hash,
            now=now,
        )

        if oauth_state is None:
            return None

        code_verifier = decrypt_data(
            oauth_state.code_verifier_encrypted
        )

        return {
            "user_id": oauth_state.user_id,
            "provider": oauth_state.provider,
            "code_verifier": code_verifier,
            "redirect_uri": oauth_state.redirect_uri,
            "consumed_at": oauth_state.consumed_at,
        }
