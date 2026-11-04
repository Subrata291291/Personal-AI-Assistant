
from pathlib import Path
from functools import lru_cache
from typing import Optional

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root directory
# backend/app/core/config.py -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """
    Central application configuration.

    Values are loaded from environment variables
    and the root .env file.
    """

    # Application
    APP_NAME: str = "Personal AI Assistant"
    APP_ENV: str = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # Security
    JWT_SECRET_KEY: Optional[SecretStr] = None
    ENCRYPTION_KEY: Optional[SecretStr] = None

    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, gt=0)

    # Supabase
    SUPABASE_URL: Optional[str] = None
    SUPABASE_ANON_KEY: Optional[SecretStr] = None
    SUPABASE_SERVICE_ROLE_KEY: Optional[SecretStr] = None
    DATABASE_URL: Optional[SecretStr] = None

    # LLM Provider Priority
    LLM_PROVIDER_PRIORITY: str = "groq,openrouter,gemini,openai"

    # Groq
    GROQ_API_KEY: Optional[SecretStr] = None
    GROQ_MODEL: Optional[str] = None
    GROQ_TIMEOUT_SECONDS: int = Field(default=20, gt=0)
    GROQ_MAX_OUTPUT_TOKENS: int = Field(default=2048, gt=0)

    # OpenRouter
    OPENROUTER_API_KEY: Optional[SecretStr] = None
    OPENROUTER_MODEL: Optional[str] = None
    OPENROUTER_TIMEOUT_SECONDS: int = Field(default=30, gt=0)
    OPENROUTER_MAX_OUTPUT_TOKENS: int = Field(default=2048, gt=0)

    # Gemini
    GEMINI_API_KEY: Optional[SecretStr] = None
    GEMINI_MODEL: Optional[str] = None
    GEMINI_TIMEOUT_SECONDS: int = Field(default=30, gt=0)
    GEMINI_MAX_OUTPUT_TOKENS: int = Field(default=2048, gt=0)

    # OpenAI
    OPENAI_API_KEY: Optional[SecretStr] = None
    OPENAI_MODEL: Optional[str] = None
    OPENAI_TIMEOUT_SECONDS: int = Field(default=30, gt=0)
    OPENAI_MAX_OUTPUT_TOKENS: int = Field(default=2048, gt=0)

    # Google OAuth
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[SecretStr] = None
    GOOGLE_REDIRECT_URI: Optional[str] = None

    # OAuth scopes (comma-separated)
    GOOGLE_OAUTH_SCOPES: str = (
        "openid,email,profile,"
        "https://www.googleapis.com/auth/gmail.readonly,"
        "https://www.googleapis.com/auth/gmail.send,"
        "https://www.googleapis.com/auth/gmail.modify,"
        "https://mail.google.com/"
    )

    # OAuth authorization settings
    GOOGLE_OAUTH_ACCESS_TYPE: str = "offline"
    GOOGLE_OAUTH_PROMPT: str = "consent"

    # Frontend and Backend
    FRONTEND_URL: str = "http://localhost:5173"
    BACKEND_URL: str = "http://localhost:8000"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def llm_provider_priority_list(self) -> list[str]:
        """Convert comma-separated provider priority into a list."""
        return [
            provider.strip().lower()
            for provider in self.LLM_PROVIDER_PRIORITY.split(",")
            if provider.strip()
        ]

    @property
    def google_oauth_scopes_list(self) -> list[str]:
        """Convert Google OAuth scopes into a list."""
        return [
            scope.strip()
            for scope in self.GOOGLE_OAUTH_SCOPES.split(",")
            if scope.strip()
        ]

@lru_cache
def get_settings() -> Settings:
    """
    Return a cached Settings instance.

    Configuration is loaded once and reused
    throughout the application.
    """
    return Settings()


settings = get_settings()
