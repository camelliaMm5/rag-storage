"""Shop database — psycopg2 connection pool for shop schema operations.

修复记录：
1. 旧版用 ``_url.split("@")`` 朴素解析 ``CONVERSATION_DB_URL``，只要 URL 不是
   ``postgresql://user:pass@host:port/db`` 这种精确形态，**import 期**就抛
   ``ValueError``（例如用 SQLite 跑离线测试时直接炸）。现在改为 urlsplit 解析 +
   延迟到首次使用才连接，且错误信息明确。
2. 电商库与 AI 会话库混用同一个环境变量，语义不清。新增 ``SHOP_DB_URL``，
   未设置时回落到旧的 ``CONVERSATION_DB_URL``（向后兼容）。
3. ``_get_pool()`` 懒加载无锁，并发首调会创建多个池；连接池耗尽时抛裸
   ``PoolError``。现已加锁 + 给出可读错误。
"""
import logging
import os
import threading
from contextlib import contextmanager
from urllib.parse import unquote, urlsplit

import psycopg2
from psycopg2.pool import PoolError, ThreadedConnectionPool

logger = logging.getLogger(__name__)

DEFAULT_URL = "postgresql://user:1234@localhost:5432/agent"
DATABASE_URL = os.getenv("SHOP_DB_URL") or os.getenv("CONVERSATION_DB_URL") or DEFAULT_URL

# 连接池容量：默认 maxconn=10 是**故意**小于线程池上限（min(32, cpu+4)）的，
# 让池耗尽成为背压信号而不是把数据库打爆；可通过环境变量按部署调大。
POOL_MINCONN = int(os.getenv("SHOP_DB_POOL_MIN", "2"))
POOL_MAXCONN = int(os.getenv("SHOP_DB_POOL_MAX", "10"))

# 每条连接都带上：
# - search_path 定向 shop schema；
# - statement_timeout 兜住慢查询 / 锁等待（与 react 层的工具超时形成双保险：
#   应用侧快速失败，数据库侧真正掐断查询并释放连接）；
# - idle_in_transaction_session_timeout 回收「忘记提交」的长事务，避免连接泄漏。
CONN_OPTIONS = (
    "-c search_path=shop,public"
    f" -c statement_timeout={int(os.getenv('SHOP_DB_STATEMENT_TIMEOUT_MS', '5000'))}"
    f" -c idle_in_transaction_session_timeout="
    f"{int(os.getenv('SHOP_DB_IDLE_TX_TIMEOUT_MS', '30000'))}"
)

_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()
_pool_kwargs: dict | None = None


def _parse_url(url: str) -> dict:
    """Parse a PostgreSQL URL into psycopg2 connection kwargs (lazy, explicit)."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme not in ("postgresql", "postgres", "postgresql+psycopg2"):
        raise RuntimeError(
            f"shop_database 仅支持 PostgreSQL 连接串，当前 SHOP_DB_URL 的 scheme 是 "
            f"{scheme!r}。若要用 SQLite 跑离线测试，请为电商库单独设置 SHOP_DB_URL。"
        )
    path = (parts.path or "").lstrip("/")
    return {
        "host": parts.hostname or "localhost",
        "port": parts.port or 5432,
        "user": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "dbname": path or "agent",
    }


def _get_pool() -> ThreadedConnectionPool:
    global _pool, _pool_kwargs
    if _pool is not None:
        return _pool
    with _pool_lock:                      # 双检加锁：并发首调只创建一个池
        if _pool is None:
            kwargs = _parse_url(DATABASE_URL)
            _pool = ThreadedConnectionPool(
                minconn=2, maxconn=10, options="-c search_path=shop,public", **kwargs
            )
            _pool_kwargs = kwargs
            logger.info("shop db pool created: %s@%s:%s/%s",
                        kwargs["user"], kwargs["host"], kwargs["port"], kwargs["dbname"])
    return _pool


def close_pool() -> None:
    """Dispose the pool (used on shutdown / in tests)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.closeall()
            finally:
                _pool = None


@contextmanager
def get_conn():
    """Context manager yielding a psycopg2 connection from the pool.

    Pool exhaustion / DB down is re-raised as a readable RuntimeError so that
    tool-layer callers can degrade gracefully instead of surfacing a bare 500.
    """
    try:
        pool = _get_pool()
    except PoolError as e:
        raise RuntimeError(f"数据库连接池不可用: {e}") from e
    except psycopg2.OperationalError as e:
        raise RuntimeError(f"无法连接数据库: {e}") from e

    try:
        conn = pool.getconn()
    except PoolError as e:
        raise RuntimeError("数据库连接池已耗尽，请稍后重试") from e

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
