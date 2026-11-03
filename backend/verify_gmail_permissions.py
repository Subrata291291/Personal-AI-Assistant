from app.db.session import SessionLocal
from app.repositories.user_connection_repository import UserConnectionRepository
from app.repositories.oauth_credential_repository import OAuthCredentialRepository

db = SessionLocal()

try:
    connection_repo = UserConnectionRepository(db)
    credential_repo = OAuthCredentialRepository(db)

    print("Enter your Personal AI Assistant user UUID:")
    user_id = input("User UUID: ").strip()

    from uuid import UUID
    user_id = UUID(user_id)

    connections = connection_repo.list_by_user(user_id=user_id)

    google_connections = [
        item for item in connections
        if item.provider == "google" and item.status == "connected"
    ]

    if not google_connections:
        print("No connected Google account found.")
    else:
        for connection in google_connections:
            credential = credential_repo.get_by_connection(
                user_id=user_id,
                connection_id=connection.id,
            )

            scopes = set(credential.scopes or []) if credential else set()

            print("\nGoogle Account:", connection.email)
            print("Gmail Read:", "gmail.readonly" in {
                scope.rsplit("/", 1)[-1] for scope in scopes
            })
            print("Gmail Send:", "gmail.send" in {
                scope.rsplit("/", 1)[-1] for scope in scopes
            })
            print("Gmail Modify:", "gmail.modify" in {
                scope.rsplit("/", 1)[-1] for scope in scopes
            })

finally:
    db.close()
