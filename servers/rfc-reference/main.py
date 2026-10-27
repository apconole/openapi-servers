#!/usr/bin/env python3
"""MCP server for referencing IETF RFCs.

Fetches RFCs from rfc-editor.org (the RFC Editor / IETF endpoint), caches
them in a local SQLite database, and exposes keyword search, title search,
and retrieval of whole RFCs, tables of contents, or individual sections.

Run over stdio; expose as an HTTP tool server with mcpo:

    mcpo --port 8001 -- python main.py
"""

from __future__ import annotations

import json
import re
import urllib.error

from mcp.server.mcpserver import MCPServer

import database as db
import fetch
import rfcparse

mcp = MCPServer(
    "rfc-reference",
    instructions=(
        "Look up IETF RFCs. Use search_rfc_titles to find RFC numbers by "
        "title/topic, get_rfc_toc to see an RFC's section outline, "
        "get_rfc to read a whole RFC or one numbered section (e.g. '5.6'), "
        "and search_rfc_content for keyword search across cached RFCs. "
        "RFC texts are downloaded from rfc-editor.org on first use and "
        "cached locally."
    ),
)

MAX_FULL_TEXT = 400_000


def _conn():
    return db.connect()


def _parse_number(value: str | int) -> int:
    m = re.search(r"(\d+)", str(value))
    if not m or not (1 <= int(m.group(1)) <= 99999):
        raise ValueError(f"could not parse an RFC number from {value!r}")
    return int(m.group(1))


def _ensure_cached(conn, number: int) -> dict:
    """Return the cached RFC row, fetching it from rfc-editor.org if needed."""
    row = db.get_cached_rfc(conn, number)
    if row is not None:
        return dict(row)
    try:
        metadata, text = fetch.fetch_rfc(number)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"RFC {number} does not exist at rfc-editor.org")
        raise
    sections = rfcparse.parse_sections(text)
    db.store_rfc(conn, number, metadata, text, sections)
    return dict(db.get_cached_rfc(conn, number))


def _index_info(conn, number: int) -> dict:
    row = conn.execute(
        "SELECT number, title, status, published, obsoleted_by "
        "FROM rfc_index WHERE number = ?", (number,)
    ).fetchone()
    return dict(row) if row else {}


def _fts_quote(query: str) -> str:
    terms = []
    for token in query.split():
        cleaned = token.replace('"', "")
        if cleaned:
            terms.append(cleaned)
    return " ".join(terms)


@mcp.tool()
def search_rfc_titles(query: str, limit: int = 20, include_obsoleted: bool = False) -> str:
    """Find RFCs by title keywords across the full RFC index (~9000+ docs).

    Returns RFC number, title, status (STD/BCP/INF/HIST/EXP), publication
    date, and obsoletion info. Run sync.py to refresh the index.

    Args:
        query: keywords, e.g. 'hypertext transfer' or 'transport security'
        limit: max results (default 20)
        include_obsoleted: also return RFCs obsoleted by others (default false)
    """
    conn = _conn()
    match = _fts_quote(query)
    rows = []
    if match:
        try:
            rows = conn.execute(
                """SELECT i.number, i.title, i.status, i.published, i.obsoleted_by,
                          bm25(rfc_index_fts) AS score
                   FROM rfc_index_fts
                   JOIN rfc_index i ON i.number = rfc_index_fts.rowid
                   WHERE rfc_index_fts MATCH ?
                   ORDER BY score LIMIT ?""",
                (match, max(1, limit)),
            ).fetchall()
        except Exception:
            rows = []
    if not rows and match:  # substring fallback for partial words
        like = f"%{match.replace('%', '').strip()}%"
        rows = conn.execute(
            """SELECT number, title, status, published, obsoleted_by, 0 AS score
               FROM rfc_index WHERE LOWER(title) LIKE ? LIMIT ?""",
            (like.lower(), max(1, limit)),
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        if not include_obsoleted and r["obsoleted_by"]:
            continue
        out.append({k: r[k] for k in
                    ("number", "title", "status", "published", "obsoleted_by")})
    return json.dumps(out, indent=2)


@mcp.tool()
def get_rfc(number: str | int, section: str | None = None,
            include_subsections: bool = True) -> str:
    """Read an RFC: the whole document, or one numbered section.

    Downloads from rfc-editor.org on first access and caches locally.
    Use get_rfc_toc first to find the right section number (e.g. '5', '5.6').

    Args:
        number: RFC number ('9110' or 'rfc9110')
        section: section number to return instead of the whole document;
                 omit for the full text
        include_subsections: when a section is given, include its nested
                             subsections (default true)
    """
    try:
        num = _parse_number(number)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    conn = _conn()
    try:
        rfc = _ensure_cached(conn, num)
    except (ValueError, urllib.error.URLError) as exc:
        conn.close()
        return json.dumps({"error": f"could not fetch RFC {num}: {exc}"})

    info = _index_info(conn, num)
    header = {
        "rfc": f"RFC{num}",
        "title": rfc["title"] or info.get("title"),
        "status": rfc["std_level"] or info.get("status"),
        "published": rfc["published"] or info.get("published"),
        "obsoleted_by": json.loads(rfc["obsoleted_by"] or "[]") or None,
        "url": rfc["url"],
        "cached_at": rfc["fetched_at"],
    }

    if section:
        sec = str(section).strip().rstrip(".")
        row = conn.execute(
            """SELECT number, title, depth, content FROM sections
               WHERE rfc_number = ? AND number = ?""",
            (num, sec),
        ).fetchone()
        conn.close()
        if row is None:
            return json.dumps({
                **header,
                "error": f"section {sec!r} not found; call get_rfc_toc for the outline",
            })
        content = row["content"]
        if not include_subsections:
            headings = rfcparse.find_headings(rfc["raw_text"])
            span = rfcparse.section_span(headings, sec, include_subsections=False)
            if span:
                content = rfcparse.clean_text(rfc["raw_text"][span[0]:span[1]])
        return json.dumps({
            **header,
            "section": row["number"],
            "section_title": row["title"],
            "content": content,
        }, indent=2)

    text = rfc["raw_text"]
    truncated = len(text) > MAX_FULL_TEXT
    conn.close()
    return json.dumps({
        **header,
        "truncated": truncated,
        "text": text[:MAX_FULL_TEXT],
        "hint": ("document truncated; prefer get_rfc_toc + section requests"
                 if truncated else None),
    }, indent=2)


@mcp.tool()
def get_rfc_toc(number: str | int) -> str:
    """Return the table of contents (numbered section outline) of an RFC.

    Args:
        number: RFC number ('9110' or 'rfc9110')
    """
    try:
        num = _parse_number(number)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    conn = _conn()
    try:
        rfc = _ensure_cached(conn, num)
    except (ValueError, urllib.error.URLError) as exc:
        conn.close()
        return json.dumps({"error": f"could not fetch RFC {num}: {exc}"})
    toc = rfcparse.table_of_contents(rfc["raw_text"])
    conn.close()
    return json.dumps({
        "rfc": f"RFC{num}",
        "title": rfc["title"],
        "toc": [{"section": t["number"], "title": t["title"]} for t in toc],
    }, indent=2)


@mcp.tool()
def search_rfc_content(query: str, rfc: str | int | None = None, limit: int = 20) -> str:
    """Keyword-search the text of locally cached RFCs, per section.

    Only searches RFCs already fetched via get_rfc/get_rfc_toc or sync.py
    --fetch; results include RFC number and section for follow-up reads.

    Args:
        query: keywords (FTS5 syntax supported: quotes, AND/OR, prefixes)
        rfc: restrict search to one RFC number
        limit: max matching sections to return (default 20)
    """
    conn = _conn()
    match = _fts_quote(query)
    if not match:
        conn.close()
        return json.dumps([])
    where = ["sections_fts MATCH ?"]
    params: list = [match]
    if rfc:
        try:
            num = _parse_number(rfc)
        except ValueError as exc:
            conn.close()
            return json.dumps({"error": str(exc)})
        where.append("s.rfc_number = ?")
        params.append(num)
    try:
        rows = conn.execute(
            f"""SELECT s.rfc_number, s.number, s.title,
                       bm25(sections_fts, 3.0, 1.0) AS score,
                       snippet(sections_fts, 1, '[', ']', '...', 20) AS snippet
                FROM sections_fts
                JOIN sections s ON s.id = sections_fts.rowid
                WHERE {' AND '.join(where)}
                ORDER BY score LIMIT ?""",
            (*params, max(1, limit)),
        ).fetchall()
    except Exception as exc:
        conn.close()
        return json.dumps({"error": f"search failed: {exc}"})
    conn.close()
    return json.dumps([{
        "rfc_number": r["rfc_number"],
        "section": r["number"],
        "section_title": r["title"],
        "relevance_score": round(-r["score"], 3),
        "snippet": r["snippet"],
    } for r in rows], indent=2)


@mcp.tool()
def list_cached_rfc() -> str:
    """List RFCs currently cached locally with title, size, and fetch time."""
    conn = _conn()
    rows = conn.execute(
        """SELECT r.number, r.title, LENGTH(r.raw_text) AS bytes,
                  (SELECT COUNT(*) FROM sections s WHERE s.rfc_number = r.number)
                      AS sections,
                  r.fetched_at,
                  COALESCE(i.status, '') AS status,
                  i.obsoleted_by
           FROM rfcs r LEFT JOIN rfc_index i ON i.number = r.number
           ORDER BY r.number"""
    ).fetchall()
    indexed = conn.execute("SELECT COUNT(*) FROM rfc_index").fetchone()[0]
    conn.close()
    return json.dumps({
        "indexed_titles": indexed,
        "cached_rfc_count": len(rows),
        "cached": [dict(r) for r in rows],
    }, indent=2)


if __name__ == "__main__":
    mcp.run()
