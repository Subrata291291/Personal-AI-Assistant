
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

from app.db.session import engine


def main():
    print("SQLAlchemy Vector compilation:")

    vector_type = Vector(768)
    print(vector_type.compile(dialect=postgresql.dialect()))

    print("\nPostgreSQL type resolution:")

    with engine.connect() as connection:
        result = connection.execute(
            text("""
                SELECT
                    current_setting('search_path') AS search_path,
                    to_regtype('vector')::text AS unqualified_vector,
                    to_regtype('extensions.vector')::text AS qualified_vector
            """)
        ).mappings().one()

        for key, value in result.items():
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
