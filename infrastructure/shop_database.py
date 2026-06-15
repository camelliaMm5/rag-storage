"""Shop database — psycopg2 connection pool for shop schema operations."""
import os
from contextlib import contextmanager

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

DATABASE_URL = os.getenv(
    "CONVERSATION_DB_URL",
    "postgresql://user:1234@localhost:5432/agent",
)

# Parse URL for connection pool
# Format: postgresql://user:pass@host:port/dbname
_url = DATABASE_URL.replace("postgresql://", "")
_user_pass, _host_db = _url.split("@")
_user, _pass = _user_pass.split(":")
_host_port, _db = _host_db.split("/")
_host, _port = _host_port.split(":") if ":" in _host_port else (_host_port, "5432")

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            minconn=2,
            maxconn=10,
            host=_host,
            port=_port,
            user=_user,
            password=_pass,
            dbname=_db,
            options="-c search_path=shop,public",
        )
    return _pool


@contextmanager
def get_conn():
    """Context manager yielding a psycopg2 connection from the pool."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def execute(sql: str, params: tuple = ()) -> int:
    """Execute a write SQL. Returns affected row count."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount


def query_one(sql: str, params: tuple = ()) -> dict | None:
    """Execute a read SQL, return one row as dict."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))


def query_all(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a read SQL, return all rows as list of dicts."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, row)) for row in rows]
