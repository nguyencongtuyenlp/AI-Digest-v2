"""
db.py — SQLite module for article history tracking.
Provides deduplication via URL hashing and persistent storage.
"""

import sqlite3
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB_PATH = os.getenv("DIGEST_DB_PATH", str(PROJECT_ROOT / "database.db"))


def _get_conn() -> sqlite3.Connection:
    """Get a connection to the SQLite database, creating the table if needed."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            url_hash    TEXT    UNIQUE NOT NULL,
            url         TEXT    NOT NULL,
            title       TEXT,
            source      TEXT,
            primary_type TEXT,
            summary     TEXT,
            full_content TEXT,
            relevance_score INTEGER,
            created_at  TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS feedback_entries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            update_id     INTEGER UNIQUE,
            chat_id       TEXT,
            message_id    INTEGER,
            user_name     TEXT,
            text          TEXT,
            labels_json   TEXT,
            created_at    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_meta (
            key           TEXT PRIMARY KEY,
            value         TEXT,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def _hash_url(url: str) -> str:
    """Generate a SHA-256 hash for a URL to enable fast duplicate lookup."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def is_duplicate(url: str) -> bool:
    """Check if an article with the given URL already exists in the database."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM articles WHERE url_hash = ?", (_hash_url(url),)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def save_article(
    url: str,
    title: str = "",
    source: str = "",
    primary_type: str = "",
    summary: str = "",
    full_content: str = "",
    relevance_score: int = 0,
) -> int:
    """
    Save an article to the database. Returns the inserted row id.
    Skips silently if the URL is already present (IGNORE on conflict).
    """
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO articles
                (url_hash, url, title, source, primary_type, summary,
                 full_content, relevance_score, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _hash_url(url),
                url,
                title,
                source,
                primary_type,
                summary,
                full_content,
                relevance_score,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def get_history(days: int = 7, limit: int = 200) -> list[dict]:
    """
    Retrieve recent articles from the last N days.
    Returns a list of dicts.
    """
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM articles
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(limit * 3, limit),),
        ).fetchall()
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered: list[dict] = []

        for row in rows:
            item = dict(row)
            created_at_raw = str(item.get("created_at", "") or "")
            try:
                created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            except ValueError:
                continue

            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at >= cutoff:
                filtered.append(item)
            if len(filtered) >= limit:
                break

        return filtered
    finally:
        conn.close()


def set_meta(key: str, value: str) -> None:
    """Lưu metadata nhỏ cho app, ví dụ last Telegram update offset."""
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO app_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_meta(key: str, default: str = "") -> str:
    """Đọc metadata nhỏ cho app."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            return default
        return str(row["value"] or default)
    finally:
        conn.close()


def save_feedback(
    *,
    update_id: int,
    chat_id: str,
    message_id: int,
    user_name: str,
    text: str,
    labels_json: str,
    created_at: str,
) -> bool:
    """Lưu feedback Telegram; trả False nếu update đã được xử lý trước đó."""
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO feedback_entries
                (update_id, chat_id, message_id, user_name, text, labels_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (update_id, chat_id, message_id, user_name, text, labels_json, created_at),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_recent_feedback(days: int = 14, limit: int = 50) -> list[dict]:
    """Lấy feedback gần đây để tạo context học tập nhẹ cho agent."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT * FROM feedback_entries
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (max(limit * 3, limit),),
        ).fetchall()

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        filtered: list[dict] = []
        for row in rows:
            item = dict(row)
            raw = str(item.get("created_at", "") or "")
            try:
                created_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue

            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            if created_at >= cutoff:
                filtered.append(item)
            if len(filtered) >= limit:
                break

        return filtered
    finally:
        conn.close()
