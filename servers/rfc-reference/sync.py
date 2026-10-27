#!/usr/bin/env python3
"""Keep the local SQLite database up to date with the RFC index.

Downloads rfc-editor.org/rfc-index.txt (title/status/date metadata for all
published RFCs) and refreshes the rfc_index table. Individual RFC texts are
cached lazily by the MCP server; use --fetch to warm the cache ahead of time.

Usage:
    python sync.py                 # refresh the title index
    python sync.py --fetch 9110 9113 2616   # also cache full text now
"""

from __future__ import annotations

import argparse
import sys

import database as db
import fetch
import rfcparse


def sync_index(conn, force: bool) -> int:
    print(f"fetching {db.INDEX_URL}")
    text = fetch.fetch_url(db.INDEX_URL).decode("utf-8", errors="replace")
    entries = db.parse_rfc_index(text)
    if not entries:
        raise SystemExit("could not parse any RFC index entries; aborting")
    count = db.upsert_index(conn, entries)
    db.set_meta(conn, "index_synced_at", db.now_iso())
    print(f"indexed {count} RFC titles")
    return count


def prefetch(conn, numbers: list[int]) -> None:
    for number in numbers:
        existing = db.get_cached_rfc(conn, number)
        if existing:
            print(f"  rfc{number}: already cached ({existing['title']})")
            continue
        metadata, text = fetch.fetch_rfc(number)
        sections = rfcparse.parse_sections(text)
        db.store_rfc(conn, number, metadata, text, sections)
        print(f"  rfc{number}: cached {len(text)} bytes, {len(sections)} sections")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help="SQLite database path (default from env)")
    parser.add_argument("--fetch", type=int, nargs="+", metavar="N",
                        help="also download and cache these RFC numbers")
    parser.add_argument("--force", action="store_true",
                        help="re-fetch even if the index looks fresh")
    args = parser.parse_args(argv)

    conn = db.connect(args.db)
    last = db.get_meta(conn, "index_synced_at")
    if last and not args.force:
        print(f"index was synced at {last}; use --force to refresh anyway")
    else:
        sync_index(conn, args.force)

    if args.fetch:
        prefetch(conn, args.fetch)

    total = conn.execute("SELECT COUNT(*) FROM rfc_index").fetchone()[0]
    cached = conn.execute("SELECT COUNT(*) FROM rfcs").fetchone()[0]
    conn.close()
    print(f"done: {total} indexed titles, {cached} full RFCs cached")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
