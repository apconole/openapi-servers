"""SQLite storage for the RFC reference MCP server.

Two kinds of data:
  * rfc_index   -- metadata (number, title, status, date) for every RFC,
                   loaded by sync.py from rfc-editor.org/rfc-index.txt
  * rfcs        -- full plain text of RFCs fetched and cached on demand,
                   plus parsed sections in a FTS5 index.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB_PATH = os.path.expanduser("~/.rfc-mcp/rfc.db")

BASE_URL = "https://www.rfc-editor.org"
INDEX_URL = f"{BASE_URL}/rfc-index.txt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rfc_index (
    number       INTEGER PRIMARY KEY,
    title        TEXT NOT NULL,
    status       TEXT,               -- e.g. 'STD 94', 'INF', 'HIST', 'EXP'
    published    TEXT,               -- 'Apr. 1969' style from the index
    obsoleted_by TEXT,               -- comma-separated RFC numbers
    updated_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_index_title ON rfc_index (title);

CREATE VIRTUAL TABLE IF NOT EXISTS rfc_index_fts USING fts5(
    title,
    content='rfc_index',
    content_rowid='number'
);

CREATE TRIGGER IF NOT EXISTS rfc_index_ai AFTER INSERT ON rfc_index BEGIN
    INSERT INTO rfc_index_fts (rowid, title) VALUES (new.number, new.title);
END;

CREATE TRIGGER IF NOT EXISTS rfc_index_ad AFTER DELETE ON rfc_index BEGIN
    INSERT INTO rfc_index_fts (rfc_index_fts, rowid, title)
    VALUES ('delete', old.number, old.title);
END;

CREATE TRIGGER IF NOT EXISTS rfc_index_au AFTER UPDATE OF title ON rfc_index BEGIN
    INSERT INTO rfc_index_fts (rfc_index_fts, rowid, title)
    VALUES ('delete', old.number, old.title);
    INSERT INTO rfc_index_fts (rowid, title) VALUES (new.number, new.title);
END;

CREATE TABLE IF NOT EXISTS rfcs (
    number      INTEGER PRIMARY KEY,
    title       TEXT,
    doc_id      TEXT,
    authors     TEXT,               -- JSON array
    published   TEXT,
    std_level   TEXT,
    obsoleted_by TEXT,              -- JSON array
    url         TEXT,
    raw_text    TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rfc_number  INTEGER NOT NULL REFERENCES rfcs(number) ON DELETE CASCADE,
    number      TEXT NOT NULL,      -- '5.6'
    title       TEXT NOT NULL,
    depth       INTEGER NOT NULL,
    content     TEXT NOT NULL,
    UNIQUE (rfc_number, number)
);

CREATE INDEX IF NOT EXISTS idx_sections_rfc ON sections (rfc_number);

CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts USING fts5(
    title, content,
    content='sections',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS sections_ai AFTER INSERT ON sections BEGIN
    INSERT INTO sections_fts (rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS sections_ad AFTER DELETE ON sections BEGIN
    INSERT INTO sections_fts (sections_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS sections_au AFTER UPDATE OF title, content ON sections BEGIN
    INSERT INTO sections_fts (sections_fts, rowid, title, content)
    VALUES ('delete', old.id, old.title, old.content);
    INSERT INTO sections_fts (rowid, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or os.environ.get("RFC_DB") or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- index

_ENTRY_START_RE = re.compile(r"^(\d{1,5}) (.*)$")
_TAG_KINDS = ("Format|Obsoletes|Obsoleted by|Updates|Updated by|Also|Status|DOI|Errata|Related")
_TAG_RE = re.compile(r"\((?:" + _TAG_KINDS + r")[^()]*\)")
_DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+(\d{4})"
)
_AUTHOR_SPLIT_RE = re.compile(r"\.\s+(?=(?:[A-Z]\.)+\s*\S)")
_STATUS_MAP = {
    "INTERNET STANDARD": "STD",
    "PROPOSED STANDARD": "PS",
    "DRAFT STANDARD": "DS",
    "BEST CURRENT PRACTICE": "BCP",
    "INFORMATIONAL": "INF",
    "EXPERIMENTAL": "EXP",
    "HISTORIC": "HIST",
}


def _parse_entry(line: str) -> dict | None:
    m = _ENTRY_START_RE.match(line)
    if not m:
        return None
    num, rest = int(m.group(1)), m.group(2)

    tags = _TAG_RE.findall(rest)
    core = " ".join(_TAG_RE.sub(" ", rest).split())

    status = None
    obsoleted_by = ""
    for tag in tags:
        inner = tag[1:-1]
        if inner.startswith("Status:"):
            status = _STATUS_MAP.get(
                inner.split(":", 1)[1].strip(), inner.split(":", 1)[1].strip()[:8]
            )
        elif inner.startswith("Also STD") or inner.startswith("Also BCP"):
            status = inner.split()[1]
        elif inner.startswith("Obsoleted by"):
            obsoleted_by = ",".join(re.findall(r"\d+", inner))

    published = None
    date_m = _DATE_RE.search(core)
    if date_m:
        months = {mm: i + 1 for i, mm in enumerate(
            ("January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"))}
        published = f"{date_m.group(2)}-{months[date_m.group(1)]:02d}"
        core_before_date = core[:date_m.start()]
    else:
        core_before_date = core

    core_before_date = core_before_date.strip().rstrip(".").strip()
    split = _AUTHOR_SPLIT_RE.split(core_before_date, maxsplit=1)
    title = split[0].strip().rstrip(".") if split and split[0].strip() else ""
    if not title:
        return None

    return {
        "number": num,
        "title": title,
        "status": status,
        "published": published,
        "obsoleted_by": obsoleted_by or None,
    }


def parse_rfc_index(text: str) -> list[dict]:
    """Parse rfc-editor.org/rfc-index.txt citations into records.

    Entries begin flush-left with the RFC number and continue on indented
    lines until a blank line. The file header contains an example citation;
    parsing only starts once numbers appear in ascending order after the
    second 'RFC INDEX' banner.
    """
    lines = text.split("\n")
    banners = [i for i, l in enumerate(lines) if l.strip() == "RFC INDEX"]
    start = (banners[-1] if banners else 0) + 1

    entries: list[dict] = []
    expected = 1
    current: str | None = None
    for line in lines[start:]:
        m = _ENTRY_START_RE.match(line)
        if m and int(m.group(1)) >= expected:
            if current is not None:
                entry = _parse_entry(current)
                if entry:
                    entries.append(entry)
                    expected = max(expected, entry["number"] + 1)
            current = line
        elif current is not None and line.strip():
            current = f"{current} {line.strip()}"
        elif current is not None and not line.strip():
            entry = _parse_entry(current)
            if entry:
                entries.append(entry)
                expected = max(expected, entry["number"] + 1)
            current = None
    if current is not None:
        entry = _parse_entry(current)
        if entry:
            entries.append(entry)
    return entries


def upsert_index(conn: sqlite3.Connection, entries: list[dict]) -> int:
    ts = now_iso()
    conn.executemany(
        """INSERT INTO rfc_index (number, title, status, published, obsoleted_by,
                                  updated_at)
           VALUES (:number, :title, :status, :published, :obsoleted_by, :ts)
           ON CONFLICT (number) DO UPDATE SET
               title = excluded.title,
               status = excluded.status,
               published = excluded.published,
               obsoleted_by = excluded.obsoleted_by,
               updated_at = excluded.updated_at""",
        [{**e, "ts": ts} for e in entries],
    )
    conn.commit()
    return len(entries)


# ---------------------------------------------------------------- rfcs

def store_rfc(conn: sqlite3.Connection, number: int, metadata: dict,
              raw_text: str, sections: list[dict]) -> None:
    """Insert or replace a cached RFC and its parsed sections."""
    conn.execute("DELETE FROM rfcs WHERE number = ?", (number,))
    conn.execute(
        """INSERT INTO rfcs (number, title, doc_id, authors, published, std_level,
                             obsoleted_by, url, raw_text, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            number,
            metadata.get("title"),
            metadata.get("doc_id") or f"RFC{number}",
            json.dumps(metadata.get("authors") or []),
            metadata.get("published"),
            metadata.get("std_level") or metadata.get("status"),
            json.dumps(metadata.get("obsoleted_by") or []),
            metadata.get("url") or f"{BASE_URL}/rfc/rfc{number}",
            raw_text,
            now_iso(),
        ),
    )
    conn.executemany(
        """INSERT OR IGNORE INTO sections (rfc_number, number, title, depth, content)
           VALUES (?, ?, ?, ?, ?)""",
        [(number, s["number"], s["title"], s["depth"], s["content"])
         for s in sections],
    )
    conn.commit()


def get_cached_rfc(conn: sqlite3.Connection, number: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM rfcs WHERE number = ?", (number,)
    ).fetchone()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None
