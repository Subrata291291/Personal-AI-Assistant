from sqlalchemy import text

from app.db.session import SessionLocal


def main():
    db = SessionLocal()

    try:
        result = db.execute(
            text("""
                SELECT
                    u.id,
                    COUNT(c.id) AS conversation_count
                FROM auth.users AS u
                LEFT JOIN public.pa_conversations AS c
                    ON c.user_id = u.id
                GROUP BY u.id
                ORDER BY conversation_count DESC
            """)
        )

        users = result.mappings().all()

        print("\nAvailable test users:")
        print("-" * 50)

        if not users:
            print("No users found.")
            return

        for user in users:
            print(
                f"User ID: {user['id']} | "
                f"Conversations: {user['conversation_count']}"
            )

        print("-" * 50)

    finally:
        db.close()


if __name__ == "__main__":
    main()
