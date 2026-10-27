"""SQLite storage layer for the mailing-lists MCP server.

Stores messages parsed from mbox files, keeps a FTS5 full-text index in
sync via triggers, and resolves reply relationships into threads.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.path.expanduser("~/.mailing-lists-mcp/lists.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    name        TEXT PRIMARY KEY,
    description TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    list_name           TEXT NOT NULL,
    kind                TEXT NOT NULL,          -- 'file' or 'url'
    location            TEXT NOT NULL,          -- absolute path or URL
    mtime               REAL,
    size                INTEGER,
    etag                TEXT,
    last_modified       TEXT,
    last_synced_at      TEXT,
    UNIQUE (kind, location)
);

CREATE TABLE IF NOT EXISTS messages (
    message_id   TEXT PRIMARY KEY,              -- normalized Message-ID (or content hash)
    content_hash TEXT NOT NULL UNIQUE,          -- catches duplicates lacking Message-ID
    list_name    TEXT,
    subject      TEXT,
    author       TEXT,
    author_email TEXT,
    date_iso     TEXT,                          -- ISO-8601 UTC, '' when unparseable
    date_ms      INTEGER NOT NULL DEFAULT 0,    -- epoch millis for range filters
    in_reply_to  TEXT,
    reply_refs   TEXT,                          -- comma-separated normalized ids
    body         TEXT,
    headers_json TEXT,
    thread_root  TEXT,
    source       TEXT,
    imported_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_list ON messages (list_name);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages (date_ms);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages (thread_root);
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages (in_reply_to);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, author, body,
    content='messages',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts (rowid, subject, author, body)
    VALUES (new.rowid, new.subject, new.author, new.body);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, subject, author, body)
    VALUES ('delete', old.rowid, old.subject, old.author, old.body);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF subject, author, body ON messages BEGIN
    INSERT INTO messages_fts (messages_fts, rowid, subject, author, body)
    VALUES ('delete', old.rowid, old.subject, old.author, old.body);
    INSERT INTO messages_fts (rowid, subject, author, body)
    VALUES (new.rowid, new.subject, new.author, new.body);
END;
"""

_ANGLE_RE = re.compile(r"^\s*<|>\s*$")
_LIST_ID_NAME_RE = re.compile(r"<([^<>]+)>")


def normalize_message_id(raw: str | None) -> str:
    """Strip whitespace and surrounding angle brackets from a Message-ID."""
    if not raw:
        return ""
    return _ANGLE_RE.sub("", raw.strip())


def parse_references(raw: str | None) -> list[str]:
    """Split a References header into normalized message ids."""
    if not raw:
        return []
    parts = re.split(r"[\s,]+", raw)
    return [normalize_message_id(p) for p in parts if p.strip()]


def content_hash(author: str, date_iso: str, subject: str, body: str) -> str:
    h = hashlib.sha256()
    for field in (author, date_iso, subject, body):
        h.update(field.encode("utf-8", "replace"))
        h.update(b"\x1e")
    return h.hexdigest()


def parse_date(value) -> datetime | None:
    """Parse a header date into an aware UTC datetime, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        from email.utils import parsedate_to_datetime

        try:
            dt = parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            try:
                dt = datetime.fromisoformat(str(value))
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def list_id_name(headers) -> str | None:
    """Extract the short list name from a List-Id header ('foo <foo.list.org>')."""
    raw = headers.get("List-Id") if hasattr(headers, "get") else None
    if not raw:
        return None
    m = _LIST_ID_NAME_RE.search(str(raw))
    return m.group(1) if m else str(raw).strip()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("MLIST_DB") or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_message(conn: sqlite3.Connection, msg: dict) -> bool:
    """Insert one message dict; returns True if it was new, False if duplicate.

    Duplicates are detected on normalized Message-ID (primary key) or on a
    content hash for messages that lack a usable Message-ID header.
    """
    message_id = normalize_message_id(msg.get("message_id"))
    author = msg.get("author") or ""
    author_email = msg.get("author_email") or ""
    subject = msg.get("subject") or ""
    body = msg.get("body") or ""

    dt = msg.get("date")
    if isinstance(dt, str):
        dt = parse_date(dt)
    date_iso = dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""
    date_ms = int(dt.timestamp() * 1000) if dt else 0

    chash = msg.get("content_hash") or content_hash(author, date_iso, subject, body)
    if not message_id:
        message_id = f"hash-{chash[:32]}"

    refs = msg.get("references") or []
    if isinstance(refs, str):
        refs = parse_references(refs)
    in_reply_to = normalize_message_id(msg.get("in_reply_to"))
    headers = msg.get("headers")
    headers_json = (
        json.dumps(dict(headers), ensure_ascii=False) if headers is not None else "{}"
    )

    cur = conn.execute(
        """INSERT OR IGNORE INTO messages
           (message_id, content_hash, list_name, subject, author, author_email,
            date_iso, date_ms, in_reply_to, reply_refs, body, headers_json,
            thread_root, source, imported_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (
            message_id,
            chash,
            msg.get("list_name"),
            subject,
            author,
            author_email,
            date_iso,
            date_ms,
            in_reply_to,
            ", ".join(refs),
            body,
            headers_json,
            msg.get("source"),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    return cur.rowcount > 0


def resolve_threads(conn: sqlite3.Connection) -> int:
    """Group messages into threads via In-Reply-To/References (union-find).

    The thread root of a component is its earliest message. Returns the
    number of rows updated.
    """
    rows = conn.execute(
        "SELECT rowid, message_id, in_reply_to, reply_refs, date_ms FROM messages"
    ).fetchall()

    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for row in rows:
        mid = row["message_id"]
        find(mid)
        links = []
        if row["in_reply_to"]:
            links.append(row["in_reply_to"])
        if row["reply_refs"]:
            links.extend(r.strip() for r in row["reply_refs"].split(",") if r.strip())
        for ref in links:
            union(ref, mid)

    components: dict[str, list[sqlite3.Row]] = {}
    by_rowid = {row["message_id"]: row for row in rows}
    for mid in by_rowid:
        components.setdefault(find(mid), []).append(by_rowid[mid])

    updates = []
    for members in components.values():
        members.sort(key=lambda r: (r["date_ms"] or 0,))
        root_id = members[0]["message_id"]
        for m in members:
            if m["message_id"] != root_id:
                updates.append((root_id, m["message_id"]))

    conn.executemany(
        "UPDATE messages SET thread_root = ? WHERE message_id = ?", updates
    )
    conn.commit()
    return len(updates)


def source_is_current(
    conn: sqlite3.Connection, kind: str, location: str,
    mtime: float | None = None, size: int | None = None,
) -> bool:
    row = conn.execute(
        "SELECT mtime, size FROM sources WHERE kind = ? AND location = ?",
        (kind, location),
    ).fetchone()
    if row is None:
        return False
    return row["mtime"] == mtime and row["size"] == size


def record_source(
    conn: sqlite3.Connection, list_name: str, kind: str, location: str,
    mtime: float | None = None, size: int | None = None,
    etag: str | None = None, last_modified: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO sources (list_name, kind, location, mtime, size, etag,
                                last_modified, last_synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (kind, location) DO UPDATE SET
               list_name = excluded.list_name,
               mtime = excluded.mtime,
               size = excluded.size,
               etag = excluded.etag,
               last_modified = excluded.last_modified,
               last_synced_at = excluded.last_synced_at""",
        (
            list_name, kind, location, mtime, size, etag, last_modified,
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    conn.commit()


def upsert_list(conn: sqlite3.Connection, name: str, source: str,
                description: str | None = None) -> None:
    conn.execute(
        """INSERT INTO lists (name, source, description) VALUES (?, ?, ?)
           ON CONFLICT (name) DO UPDATE SET
               source = COALESCE(excluded.source, lists.source),
               description = COALESCE(excluded.description, lists.description)""",
        (name, source, description),
    )
    conn.commit()


def row_to_message(row: sqlite3.Row, *, include_body: bool = True) -> dict:
    msg = {
        "message_id": row["message_id"],
        "list_name": row["list_name"],
        "subject": row["subject"],
        "author": row["author"],
        "author_email": row["author_email"],
        "date": row["date_iso"],
        "in_reply_to": row["in_reply_to"],
        "references": [r.strip() for r in (row["reply_refs"] or "").split(",") if r.strip()],
        "thread_root": row["thread_root"],
    }
    if include_body:
        msg["body"] = row["body"]
    return msg
