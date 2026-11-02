
import sys
from pathlib import Path

# Add backend directory to Python import path
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import text
from app.db.session import engine


QUERY = text("""
    SELECT
        current_user AS db_user,
        session_user AS session_user,
        r.rolsuper,
        r.rolbypassrls,
        has_table_privilege(
            current_user,
            'public.pa_chat_requests',
            'SELECT'
        ) AS can_select,
        has_table_privilege(
            current_user,
            'public.pa_chat_requests',
            'INSERT'
        ) AS can_insert,
        has_table_privilege(
            current_user,
            'public.pa_chat_requests',
            'UPDATE'
        ) AS can_update,
        has_table_privilege(
            current_user,
            'public.pa_chat_requests',
            'DELETE'
        ) AS can_delete
    FROM pg_roles AS r
    WHERE r.rolname = current_user
""")


def main():
    try:
        with engine.connect() as connection:
            result = connection.execute(QUERY).mappings().one()

            print("\nDatabase Role Verification")
            print("-" * 35)

            for key, value in result.items():
                print(f"{key}: {value}")

            print("-" * 35)

    except Exception as error:
        print(f"Database verification failed: {type(error).__name__}")
        raise


if __name__ == "__main__":
    main()
