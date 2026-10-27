"""HTTP helpers for fetching RFC data from rfc-editor.org."""

from __future__ import annotations

import gzip
import io
import json
import urllib.error
import urllib.request

BASE_URL = "https://www.rfc-editor.org"
USER_AGENT = "rfc-reference-mcp/1.0 (+https://github.com/open-webui/openapi-servers)"


def fetch_url(url: str, timeout: int = 60) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.GzipFile(fileobj=io.BytesIO(data)).read()
        return data


def fetch_rfc_text(number: int) -> str:
    url = f"{BASE_URL}/rfc/rfc{number}.txt"
    return fetch_url(url).decode("utf-8", errors="replace")


def fetch_rfc_metadata(number: int) -> dict:
    """Fetch per-RFC JSON metadata; returns {} if unavailable."""
    url = f"{BASE_URL}/rfc/rfc{number}.json"
    try:
        data = json.loads(fetch_url(url).decode("utf-8", errors="replace"))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return {}
    return {
        "title": data.get("title"),
        "doc_id": data.get("doc_id") or f"RFC{number}",
        "authors": data.get("authors") or [],
        "published": data.get("published"),
        "std_level": (data.get("std_level") or "").upper() or None,
        "obsoleted_by": data.get("obsoleted_by") or [],
        "url": f"{BASE_URL}/rfc/rfc{number}",
    }


def fetch_rfc(number: int) -> tuple[dict, str]:
    """Return (metadata, raw_text) for an RFC number."""
    return fetch_rfc_metadata(number), fetch_rfc_text(number)
