from collections.abc import Mapping
import json
import logging
from pathlib import Path
import sqlite3
import time
from typing import Any

logger = logging.getLogger("github_project_hunter.cache")


class PersistentCache:
    """Lightweight SQLite-backed persistent cache with TTL expiration."""

    def __init__(self, cache_dir: str | Path = ".cache", db_name: str = "github_cache.db"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.cache_dir / db_name
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_cache (
                        cache_key TEXT PRIMARY KEY,
                        expires_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_expires_at ON api_cache(expires_at)"
                )
        except sqlite3.Error as exc:
            logger.warning("Failed to initialize persistent cache DB: %s", exc)

    def get(self, key: str, now: float | None = None) -> Any | None:
        current_time = time.time() if now is None else now
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "SELECT payload, expires_at FROM api_cache WHERE cache_key = ?",
                    (key,),
                )
                row = cursor.fetchone()
                if row:
                    payload_text, expires_at = row["payload"], row["expires_at"]
                    if expires_at > current_time:
                        return json.loads(payload_text)
                    # Expired: clean it up
                    conn.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,))
        except Exception as exc:
            logger.warning("Persistent cache get error for key %s: %s", key, exc)
        return None

    def set(self, key: str, payload: Any, ttl_seconds: float, now: float | None = None) -> None:
        if ttl_seconds <= 0:
            return
        current_time = time.time() if now is None else now
        expires_at = current_time + ttl_seconds
        try:
            payload_text = json.dumps(payload)
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO api_cache (cache_key, expires_at, created_at, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        expires_at = excluded.expires_at,
                        created_at = excluded.created_at,
                        payload = excluded.payload
                    """,
                    (key, expires_at, current_time, payload_text),
                )
        except Exception as exc:
            logger.warning("Persistent cache set error for key %s: %s", key, exc)

    def delete(self, key: str) -> bool:
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("DELETE FROM api_cache WHERE cache_key = ?", (key,))
                return cursor.rowcount > 0
        except Exception as exc:
            logger.warning("Persistent cache delete error for key %s: %s", key, exc)
            return False

    def clear(self) -> int:
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("DELETE FROM api_cache")
                return cursor.rowcount
        except Exception as exc:
            logger.warning("Persistent cache clear error: %s", exc)
            return 0

    def cleanup_expired(self, now: float | None = None) -> int:
        current_time = time.time() if now is None else now
        try:
            with self._get_connection() as conn:
                cursor = conn.execute(
                    "DELETE FROM api_cache WHERE expires_at <= ?", (current_time,)
                )
                return cursor.rowcount
        except Exception as exc:
            logger.warning("Persistent cache cleanup error: %s", exc)
            return 0

    def count(self) -> int:
        try:
            with self._get_connection() as conn:
                cursor = conn.execute("SELECT COUNT(*) AS total FROM api_cache")
                row = cursor.fetchone()
                return int(row["total"]) if row else 0
        except Exception:
            return 0
