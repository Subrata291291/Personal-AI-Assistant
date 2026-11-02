
from sqlalchemy import text

from app.db.session import engine


def main():
    with engine.connect() as connection:
        rows = connection.execute(
            text("""
                SELECT id, created_at
                FROM public.pa_conversations
                ORDER BY created_at DESC
                LIMIT 5
            """)
        ).mappings().all()

    if not rows:
        print("No existing conversations found.")
        return

    print("\nExisting conversations:\n")

    for index, row in enumerate(rows, start=1):
        print(f"{index}. Conversation ID: {row['id']}")
        print(f"   Created at: {row['created_at']}")
        print()


if __name__ == "__main__":
    main()
