#!/usr/bin/env python3
"""Keep a local SQLite database up to date with mailing list mbox archives.

Imports plain and gzipped mbox files from local paths (files or directories)
and downloads gzipped mbox archives from configured URLs. Duplicate messages
are silently ignored, so re-running is always safe.

Usage:
    python sync.py --config config.yaml
    python sync.py --list lkml --path /srv/archives/lkml --list xorg --url https://lists.example.org/xorg.mbox.gz
"""

from __future__ import annotations

import argparse
import email
import gzip
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from html.parser import HTMLParser

import yaml

import database as db


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "pre"}

    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self.chunks.append(data)

    def handle_starttag(self, tag, attrs) -> None:
        if tag in self.BLOCK_TAGS:
            self.chunks.append("\n")

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.chunks)).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    try:
        parser.feed(html)
    except Exception:
        return html
    return parser.text()


def _decode_part(part) -> str:
    if hasattr(part, "get_content"):
        try:
            content = part.get_content()
            if isinstance(content, bytes):
                content = content.decode(part.get_content_charset() or "utf-8",
                                         errors="replace")
            return content
        except Exception:
            pass
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload()) or ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def extract_body(part) -> str:
    """Get a plain-text body from a MIME part, decoding payloads."""
    ctype = part.get_content_type()
    if ctype == "text/plain":
        return _decode_part(part)
    if ctype == "text/html":
        return html_to_text(_decode_part(part))
    if part.is_multipart():
        plain, html = [], []
        for sub in part.get_payload():
            text = extract_body(sub)
            if sub.get_content_type() == "text/plain":
                plain.append(text)
            elif sub.get_content_type() == "text/html":
                html.append(text)
        body = "\n".join(plain).strip() or "\n".join(html).strip()
        return body
    return ""


def message_to_dict(msg, list_name: str | None, source: str) -> dict | None:
    from email.utils import parseaddr

    author_name, author_email = parseaddr(str(msg.get("From") or ""))
    refs = msg.get("References")
    return {
        "message_id": db.normalize_message_id(msg.get("Message-ID")),
        "list_name": list_name or db.list_id_name(msg),
        "subject": str(msg.get("Subject") or "").strip(),
        "author": author_name or author_email,
        "author_email": author_email,
        "date": db.parse_date(msg.get("Date")),
        "in_reply_to": msg.get("In-Reply-To"),
        "references": db.parse_references(refs),
        "body": extract_body(msg),
        "headers": {k: str(v) for k, v in msg.items()},
        "source": source,
    }


def _is_gzip(path: str) -> bool:
    with open(path, "rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def open_mbox(path: str):
    """Return (file_handle, temp_path_or_None) reading plain or gzipped mbox."""
    if _is_gzip(path):
        return gzip.open(path, "rb"), None
    return open(path, "rb"), None


def iter_mbox_messages(binary_fh):
    """Yield email.message.Message objects from a binary mbox stream."""
    from email.policy import default as email_policy

    current: list[bytes] = []
    for line in binary_fh:
        if line.startswith(b"From "):
            if current:
                yield _parse_bytes(b"".join(current), email_policy)
            current = []
        elif line.startswith(b">") and line[1:].startswith(b"From "):
            line = line[1:]  # unescape quoted From_ lines
        current.append(line)
    if current:
        yield _parse_bytes(b"".join(current), email_policy)


def _parse_bytes(data: bytes, policy):
    msg = email.message_from_bytes(data, policy=policy)
    return None if not msg.items() else msg


def import_mbox_path(conn, path: str, list_name: str | None) -> tuple[int, int]:
    """Import one mbox file. Returns (new, duplicate) counts."""
    path = os.path.abspath(path)
    new = dup = 0
    fh, _tmp = open_mbox(path)
    try:
        for raw in iter_mbox_messages(fh):
            msg = message_to_dict(raw, list_name, path)
            if msg is None:
                continue
            if db.insert_message(conn, msg):
                new += 1
            else:
                dup += 1
    finally:
        fh.close()
    conn.commit()
    return new, dup


def expand_local_path(path: str) -> list[str]:
    path = os.path.expanduser(path)
    if os.path.isdir(path):
        found = []
        for entry in sorted(os.listdir(path)):
            if ".mbox" in entry or entry.endswith(".gz"):
                full = os.path.join(path, entry)
                if os.path.isfile(full):
                    found.append(full)
        return found
    return [path] if os.path.isfile(path) else []


def sync_local(conn, name: str, path: str, force: bool) -> tuple[int, int]:
    total_new = total_dup = 0
    files = expand_local_path(path)
    if not files:
        print(f"  [{name}] no mbox files found at {path}", file=sys.stderr)
    for file_path in files:
        st = os.stat(file_path)
        if not force and db.source_is_current(
            conn, "file", file_path, mtime=st.st_mtime, size=st.st_size
        ):
            print(f"  [{name}] up to date: {os.path.basename(file_path)}")
            continue
        new, dup = import_mbox_path(conn, file_path, name)
        db.record_source(
            conn, name, "file", file_path, mtime=st.st_mtime, size=st.st_size
        )
        total_new += new
        total_dup += dup
        print(f"  [{name}] {os.path.basename(file_path)}: {new} new, {dup} duplicates")
    return total_new, total_dup


def sync_url(conn, name: str, url: str, force: bool) -> tuple[int, int]:
    row = conn.execute(
        "SELECT etag, last_modified FROM sources WHERE kind = 'url' AND location = ?",
        (url,),
    ).fetchone()

    request = urllib.request.Request(url, headers={"User-Agent": "mailing-lists-mcp/1.0"})
    if row and not force:
        if row["etag"]:
            request.add_header("If-None-Match", row["etag"])
        if row["last_modified"]:
            request.add_header("If-Modified-Since", row["last_modified"])

    print(f"  [{name}] fetching {url}")
    tmp_path = None
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            fd, tmp_path = tempfile.mkstemp(suffix=".mbox.gz")
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(resp, out)
            etag = resp.headers.get("ETag")
            last_modified = resp.headers.get("Last-Modified")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            print(f"  [{name}] up to date (304 Not Modified)")
            return 0, 0
        raise

    try:
        new, dup = import_mbox_path(conn, tmp_path, name)
    finally:
        os.unlink(tmp_path)
    db.record_source(conn, name, "url", url, etag=etag, last_modified=last_modified)
    print(f"  [{name}] {new} new, {dup} duplicates")
    return new, dup


def load_config(path: str) -> dict:
    with open(path) as fh:
        cfg = yaml.safe_load(fh) or {}
    if not isinstance(cfg.get("lists"), list):
        raise SystemExit(f"{path}: expected a top-level 'lists' list")
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", help="YAML config file with lists and db path")
    parser.add_argument("--db", help="SQLite database path (default from config/env)")
    parser.add_argument("--list", dest="cli_lists", action="append", default=[],
                        metavar="NAME", help="name for a --path/--url source (repeatable)")
    parser.add_argument("--path", dest="cli_path", help="local mbox file or directory")
    parser.add_argument("--url", dest="cli_url", help="URL of a (gzipped) mbox archive")
    parser.add_argument("--force", action="store_true",
                        help="re-import sources even if unchanged")
    args = parser.parse_args(argv)

    entries: list[dict] = []
    db_path = args.db
    if args.config:
        cfg = load_config(args.config)
        db_path = args.db or cfg.get("db")
        entries.extend(cfg["lists"])
    if args.cli_lists and len(args.cli_lists) == 1 and (args.cli_path or args.cli_url):
        entries.append({
            "name": args.cli_lists[0],
            "path": args.cli_path,
            "url": args.cli_url,
        })
    elif args.cli_lists or args.cli_path or args.cli_url:
        raise SystemExit("--list/--path/--url: use exactly one --list with --path or --url")
    if not entries:
        parser.error("nothing to sync: provide --config or --list with --path/--url")

    conn = db.connect(db_path)
    grand_new = grand_dup = 0
    for entry in entries:
        name = entry.get("name")
        if not name:
            print(f"skipping entry without a name: {entry}", file=sys.stderr)
            continue
        db.upsert_list(conn, name, entry.get("url") or entry.get("path"),
                       entry.get("description"))
        if entry.get("path"):
            new, dup = sync_local(conn, name, entry["path"], args.force)
        elif entry.get("url"):
            new, dup = sync_url(conn, name, entry["url"], args.force)
        else:
            print(f"  [{name}] entry has neither 'path' nor 'url', skipping",
                  file=sys.stderr)
            continue
        grand_new += new
        grand_dup += dup

    updated = db.resolve_threads(conn)
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    threads = conn.execute(
        "SELECT COUNT(DISTINCT COALESCE(thread_root, message_id)) FROM messages"
    ).fetchone()[0]
    conn.close()
    print(f"done: {grand_new} new, {grand_dup} duplicates ignored; "
          f"{total} messages in {threads} threads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
