from supabase import create_client

from app.core.config import settings


if not settings.SUPABASE_URL or not settings.SUPABASE_ANON_KEY:
    raise RuntimeError("Supabase configuration is missing.")

supabase = create_client(
    settings.SUPABASE_URL,
    settings.SUPABASE_ANON_KEY.get_secret_value(),
)

email = input("Supabase email: ").strip()
password = input("Supabase password: ")

response = supabase.auth.sign_in_with_password({
    "email": email,
    "password": password,
})

if not response.session:
    raise RuntimeError("Login failed. No access token returned.")

access_token = response.session.access_token

with open(".access_token.tmp", "w", encoding="utf-8") as file:
    file.write(access_token)

print("\nLogin successful!")
print("Access token saved locally.")
print("Token expires in:", response.session.expires_in, "seconds")