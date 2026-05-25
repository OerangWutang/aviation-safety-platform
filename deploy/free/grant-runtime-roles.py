from __future__ import annotations

import os

import psycopg2
from psycopg2 import sql


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is required")
    return value


def main() -> None:
    database_url = _required("DATABASE_SYNC_URL")
    db_name = _required("POSTGRES_DB")
    roles = [_required("ATLAS_TENANT_DB_USER"), _required("ATLAS_SYSTEM_DB_USER")]

    with psycopg2.connect(database_url) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            for role in roles:
                ident = sql.Identifier(role)
                cur.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(db_name), ident
                    )
                )
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(ident))
                cur.execute(
                    sql.SQL(
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
                    ).format(ident)
                )
                cur.execute(
                    sql.SQL(
                        "GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {}"
                    ).format(ident)
                )
                cur.execute(
                    sql.SQL("GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {}").format(ident)
                )
                cur.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}"
                    ).format(ident)
                )
                cur.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
                        "GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {}"
                    ).format(ident)
                )
                cur.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT EXECUTE ON FUNCTIONS TO {}"
                    ).format(ident)
                )


if __name__ == "__main__":
    main()
