
from app.integrations.google.api_client import GoogleAPIClient


client = GoogleAPIClient()


# Valid Google API URLs
valid_urls = [
    "https://gmail.googleapis.com/gmail/v1/users/me/messages",
    "https://www.googleapis.com/oauth2/v3/userinfo",
    "https://calendar.googleapis.com/calendar/v3/users/me/calendarList",
    "https://drive.googleapis.com/drive/v3/files",
    "https://people.googleapis.com/v1/people/me",
]

for url in valid_urls:
    client._validate_url(url)

print("Approved Google hosts: PASSED")


# Invalid URLs
invalid_urls = [
    "http://gmail.googleapis.com/gmail/v1/users/me/messages",
    "https://evil.example.com/api",
    "https://gmail.googleapis.com:8443/api",
    "https://user:password@gmail.com.googleapis.com/api",
    "https://gmail.googleapis.com/api#fragment",
]

for url in invalid_urls:
    try:
        client._validate_url(url)
    except ValueError:
        continue
    else:
        raise AssertionError(f"Unsafe URL accepted: {url}")

print("Unsafe URL rejection: PASSED")


# Invalid HTTP methods
for method in ["TRACE", "OPTIONS", "CONNECT"]:
    assert method not in client.ALLOWED_METHODS

print("HTTP method allowlist: PASSED")

print("Google API URL security verification: PASSED")
