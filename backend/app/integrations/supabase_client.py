
from supabase import Client, create_client

from app.core.config import settings


def create_user_supabase_client(access_token: str) -> Client:
    """
    Create a Supabase client scoped to one authenticated user.

    The user's access token is attached to the client.
    Never share this client between different users.
    """

    if not settings.SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL is not configured.")

    if not settings.SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_ANON_KEY is not configured.")

    if not access_token or not access_token.strip():
        raise ValueError("A valid user access token is required.")

    client = create_client(
        settings.SUPABASE_URL,
        settings.SUPABASE_ANON_KEY.get_secret_value(),
    )

    client.postgrest.auth(access_token)

    return client
