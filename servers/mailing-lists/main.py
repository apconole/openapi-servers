#!/usr/bin/env python3
"""MCP server exposing read access to mailing list discussions.

Tools search and browse discussions previously imported into the local
SQLite database by sync.py. Runs over stdio; expose it as an HTTP/OpenAPI
tool server for e.g. Open WebUI with mcpo:

    mcpo --port 8000 -- python main.py
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from mcp.server.mcpserver import MCPServer

import database as db

mcp = MCPServer(
    "mailing-lists",
    instructions=(
        "Search and read archived mailing list discussions. Use "
        "list_available_lists to see what is archived, search_discussions to "
        "find threads about a topic, get_thread or get_message to read them, "
        "and list_messages to browse mail by date, list, author, or subject."
    ),
)


def _conn():
    return db.connect()


def _parse_date_bound(value: str | None, *, end_of_day: bool = False) -> int | None:
    """Convert an ISO date/datetime string to epoch millis."""
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"could not parse date {value!r}; use YYYY-MM-DD or ISO-8601")
    if dt.tzinfo is None:
        if end_of_day and len(text) <= 10:
            dt = dt.replace(hour=23, minute=59, second=59)
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _fts_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression from free-text words/phrases."""
    terms = []
    for token in query.split():
        if token.startswith('"') and token.endswith('"') and len(token) > 1:
            terms.append(token)
        else:
            cleaned = token.replace('"', "")
            if cleaned:
                terms.append(f'"{cleaned}"' if " " in cleaned else cleaned)
    return " ".join(terms) + ("*" if terms and not terms[-1].startswith('"') else "")


@mcp.tool()
def list_available_lists() -> str:
    """List the mailing lists available in the local archive with counts."""
    conn = _conn()
    rows = conn.execute(
        """SELECT l.name, l.source,
                  COUNT(m.message_id) AS message_count,
                  MIN(NULLIF(m.date_iso, '')) AS first_date,
                  MAX(NULLIF(m.date_iso, '')) AS last_date
           FROM lists l
           LEFT JOIN messages m ON m.list_name = l.name
           GROUP BY l.name ORDER BY l.name"""
    ).fetchall()
    orphans = conn.execute(
        """SELECT list_name, COUNT(*) c FROM messages
           WHERE list_name IS NOT NULL
             AND list_name NOT IN (SELECT name FROM lists)
           GROUP BY list_name"""
    ).fetchall()
    conn.close()
    result = [dict(r) for r in rows]
    result.extend(
        {"name": r["list_name"], "source": None, "message_count": r["c"],
         "first_date": None, "last_date": None}
        for r in orphans
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def search_discussions(
    query: str,
    list_name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 10,
) -> str:
    """Full-text search archived mail and return matching discussions.

    Results are grouped by thread: one entry per thread with a relevance
    score, snippet, message count, participants, and date range. Use
    get_thread with the root_message_id to read a discussion.

    Args:
        query: keywords (FTS5 syntax supported: quotes, AND/OR, prefixes like 'net*')
        list_name: restrict to one mailing list
        since/until: ISO date bounds, e.g. '2024-01-01'
        limit: max threads to return (default 10)
    """
    conn = _conn()
    match = _fts_query(query)
    where = ["messages_fts MATCH ?"]
    params: list = [match]
    if list_name:
        where.append("m.list_name = ?")
        params.append(list_name)
    since_ms = _parse_date_bound(since)
    until_ms = _parse_date_bound(until, end_of_day=True)
    if since_ms is not None:
        where.append("m.date_ms >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("m.date_ms <= ?")
        params.append(until_ms)
    where_sql = " AND ".join(where)

    rows = conn.execute(
        f"""SELECT m.message_id,
                   COALESCE(m.thread_root, m.message_id) AS root,
                   bm25(messages_fts, 5.0, 1.0, 1.0) AS score,
                   snippet(messages_fts, 2, '[', ']', '...', 24) AS snippet
            FROM messages_fts
            JOIN messages m ON m.rowid = messages_fts.rowid
            WHERE {where_sql}
            ORDER BY score
            LIMIT 500""",
        params,
    ).fetchall()
    conn.close()

    threads: dict[str, dict] = {}
    for row in rows:
        entry = threads.setdefault(row["root"], {
            "root_message_id": row["root"],
            "relevance_score": round(-row["score"], 3),
            "matched_messages": 0,
            "_best_snippet": row["snippet"],
            "_ids": set(),
        })
        entry["matched_messages"] += 1
        entry["_ids"].add(row["message_id"])

    ranked = sorted(threads.values(), key=lambda t: t["relevance_score"])[: max(1, limit)]
    if not ranked:
        return json.dumps([])

    conn = _conn()
    out = []
    for entry in ranked:
        root = entry["root_message_id"]
        ids = entry.pop("_ids")
        snippet = entry.pop("_best_snippet")
        info = conn.execute(
            """SELECT COALESCE((SELECT subject FROM messages WHERE message_id = ?),
                               MIN(NULLIF(subject, ''))) AS subject,
                      COUNT(*) AS message_count,
                      MIN(NULLIF(date_iso, '')) AS first_date,
                      MAX(NULLIF(date_iso, '')) AS last_date,
                      GROUP_CONCAT(DISTINCT author) AS participants,
                      MIN(list_name) AS list_name
               FROM messages WHERE thread_root = ? OR message_id = ?""",
            (root, root, root),
        ).fetchone()
        entry.update({
            "subject": info["subject"],
            "list_name": info["list_name"],
            "message_count": info["message_count"],
            "first_date": info["first_date"],
            "last_date": info["last_date"],
            "participants": sorted({p for p in (info["participants"] or "").split(",") if p}),
            "matched_message_ids": sorted(ids)[:10],
            "snippet": snippet,
        })
        out.append(entry)
    conn.close()
    return json.dumps(out, indent=2)


@mcp.tool()
def get_message(message_id: str) -> str:
    """Fetch one message in full by its Message-ID (angle brackets optional).

    Args:
        message_id: the Message-ID header value, e.g. '<abc123@lists.example>'
    """
    conn = _conn()
    mid = db.normalize_message_id(message_id)
    row = conn.execute(
        "SELECT * FROM messages WHERE message_id = ?", (mid,)
    ).fetchone()
    if row is None:
        like = conn.execute(
            "SELECT message_id FROM messages WHERE message_id LIKE ? LIMIT 5",
            (f"%{mid}%",),
        ).fetchall()
        conn.close()
        return json.dumps({
            "error": f"message {mid!r} not found",
            "did_you_mean": [r["message_id"] for r in like],
        })
    result = db.row_to_message(row, include_body=True)
    conn.close()
    return json.dumps(result, indent=2)


@mcp.tool()
def get_thread(message_id: str, max_messages: int = 100) -> str:
    """Read a whole discussion (thread) chronologically, given any message in it.

    Args:
        message_id: Message-ID of any message belonging to the thread
        max_messages: cap on returned messages (default 100)
    """
    conn = _conn()
    mid = db.normalize_message_id(message_id)
    row = conn.execute(
        "SELECT COALESCE(thread_root, message_id) AS root FROM messages WHERE message_id = ?",
        (mid,),
    ).fetchone()
    if row is None:
        conn.close()
        return json.dumps({"error": f"message {mid!r} not found"})
    rows = conn.execute(
        """SELECT * FROM messages
           WHERE thread_root = ? OR message_id = ?
           ORDER BY date_ms, rowid LIMIT ?""",
        (row["root"], row["root"], max(1, max_messages)),
    ).fetchall()
    conn.close()
    return json.dumps({
        "thread_root": row["root"],
        "message_count": len(rows),
        "subject": rows[0]["subject"] if rows else None,
        "messages": [db.row_to_message(r) for r in rows],
    }, indent=2)


@mcp.tool()
def list_messages(
    list_name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    author: str | None = None,
    subject_contains: str | None = None,
    thread_root: str | None = None,
    offset: int = 0,
    limit: int = 25,
) -> str:
    """Browse archived messages by date, list, author, subject, or thread.

    Returns message metadata (no bodies; use get_message for the full text).

    Args:
        list_name: filter by mailing list
        since/until: ISO date bounds, e.g. '2024-03-01'
        author: substring match on author name or email
        subject_contains: substring match on subject
        thread_root: Message-ID of a thread root to list all replies under
        offset/limit: pagination (default limit 25)
    """
    conn = _conn()
    where, params = ["1=1"], []
    if list_name:
        where.append("list_name = ?")
        params.append(list_name)
    since_ms = _parse_date_bound(since)
    until_ms = _parse_date_bound(until, end_of_day=True)
    if since_ms is not None:
        where.append("date_ms >= ?")
        params.append(since_ms)
    if until_ms is not None:
        where.append("date_ms <= ?")
        params.append(until_ms)
    if author:
        where.append("(LOWER(author) LIKE ? OR LOWER(author_email) LIKE ?)")
        params.extend([f"%{author.lower()}%"] * 2)
    if subject_contains:
        where.append("LOWER(subject) LIKE ?")
        params.append(f"%{subject_contains.lower()}%")
    if thread_root:
        root = db.normalize_message_id(thread_root)
        where.append("(thread_root = ? OR message_id = ?)")
        params.extend([root, root])
    where_sql = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""SELECT * FROM messages WHERE {where_sql}
            ORDER BY date_ms DESC, rowid DESC LIMIT ? OFFSET ?""",
        (*params, max(1, limit), max(0, offset)),
    ).fetchall()
    conn.close()
    return json.dumps({
        "total": total,
        "offset": offset,
        "returned": len(rows),
        "messages": [db.row_to_message(r, include_body=False) for r in rows],
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
