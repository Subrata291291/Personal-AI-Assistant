
import base64
import binascii

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, status
import os

from app.core.config import settings


# ==========================================
# JWT AUTHENTICATION
# ==========================================

def _get_jwt_secret() -> str:
    """Get and validate the JWT signing secret."""

    if not settings.JWT_SECRET_KEY:
        raise RuntimeError("JWT_SECRET_KEY is not configured.")

    secret = settings.JWT_SECRET_KEY.get_secret_value()

    if len(secret) < 32:
        raise RuntimeError(
            "JWT_SECRET_KEY must contain at least 32 characters."
        )

    return secret


def create_access_token(
    subject: str,
    expires_delta: Optional[timedelta] = None,
) -> str:
    """
    Create a signed JWT access token.

    The subject should identify the authenticated user.
    """

    if not subject or not subject.strip():
        raise ValueError("Token subject cannot be empty.")

    now = datetime.now(timezone.utc)

    expires_at = now + (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )

    payload = {
        "sub": subject,
        "iat": now,
        "exp": expires_at,
    }

    return jwt.encode(
        payload,
        _get_jwt_secret(),
        algorithm=settings.JWT_ALGORITHM,
    )


def decode_access_token(token: str) -> dict:
    """Verify a JWT and return its claims."""

    try:
        return jwt.decode(
            token,
            _get_jwt_secret(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
        )

    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ==========================================
# AES-256-GCM ENCRYPTION
# ==========================================

def _get_encryption_key() -> bytes:
    """Decode and validate the 32-byte AES encryption key."""

    if not settings.ENCRYPTION_KEY:
        raise RuntimeError("ENCRYPTION_KEY is not configured.")

    encoded_key = settings.ENCRYPTION_KEY.get_secret_value()

    try:
        key = base64.b64decode(
            encoded_key,
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, binascii.Error):
        raise RuntimeError(
            "ENCRYPTION_KEY must be valid URL-safe Base64."
        )

    if len(key) != 32:
        raise RuntimeError(
            "ENCRYPTION_KEY must decode to exactly 32 bytes."
        )

    return key


def encrypt_data(plaintext: str) -> str:
    """
    Encrypt text using AES-256-GCM.

    Returns URL-safe Base64 containing nonce + ciphertext.
    """

    if not isinstance(plaintext, str):
        raise TypeError("Plaintext must be a string.")

    key = _get_encryption_key()

    # AES-GCM requires a unique nonce for each encryption.

    nonce = os.urandom(12)

    aesgcm = AESGCM(key)

    ciphertext = aesgcm.encrypt(
        nonce,
        plaintext.encode("utf-8"),
        None,
    )

    encrypted_payload = nonce + ciphertext

    return base64.urlsafe_b64encode(
        encrypted_payload
    ).decode("utf-8")


def decrypt_data(encrypted_data: str) -> str:
    """Decrypt data previously encrypted by encrypt_data()."""

    try:
        payload = base64.urlsafe_b64decode(encrypted_data)

        if len(payload) < 29:
            raise ValueError("Invalid encrypted payload.")

        nonce = payload[:12]
        ciphertext = payload[12:]

        aesgcm = AESGCM(_get_encryption_key())

        plaintext = aesgcm.decrypt(
            nonce,
            ciphertext,
            None,
        )

        return plaintext.decode("utf-8")

    except Exception as exc:
        raise ValueError(
            "Unable to decrypt data. Check the encryption key "
            "and encrypted payload."
        ) from exc
