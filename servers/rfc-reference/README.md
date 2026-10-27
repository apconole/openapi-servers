# RFC Reference MCP Server

An MCP server that gives LLMs structured access to IETF RFCs. RFC text is
fetched from the RFC Editor (`rfc-editor.org`, the IETF RFC endpoint) and
cached in a local SQLite database, with FTS5 keyword search over both the
full RFC title index (~10,000 documents) and the section-level text of
cached RFCs. Retrieve a whole RFC, its table of contents, or individual
numbered sections (e.g. `5.6`).

## Components

| File | Role |
|---|---|
| `sync.py` | CLI: refreshes the RFC title index; optional `--fetch` to pre-cache RFCs |
| `main.py` | MCP server (stdio) with search/retrieval tools |
| `database.py` | SQLite schema, index parsing, caching |
| `rfcparse.py` | Section/TOC parser for RFC plain-text layout |
| `fetch.py` | HTTP helpers for rfc-editor.org |

## Setup

```bash
cd servers/rfc-reference
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python sync.py        # download the RFC title index (~2 MB)
```

Database path: `--db` flag, `RFC_DB` env var, or default
`~/.rfc-mcp/rfc.db`. Optionally warm the full-text cache:

```bash
.venv/bin/python sync.py --fetch 9110 9113 8446
```

Individual RFCs are otherwise fetched and cached automatically the first
time a tool asks for them, so the cache grows with use and no cron job is
required (run `sync.py --force` occasionally to refresh titles/status).

## Run the MCP server

```bash
RFC_DB=/path/to/rfc.db .venv/bin/python main.py    # stdio transport
```

MCP client config:

```json
{
  "mcpServers": {
    "rfc-reference": {
      "command": "/path/to/servers/rfc-reference/.venv/bin/python",
      "args": ["/path/to/servers/rfc-reference/main.py"],
      "env": { "RFC_DB": "/home/you/.rfc-mcp/rfc.db" }
    }
  }
}
```

### Tools

| Tool | Purpose |
|---|---|
| `search_rfc_titles(query, limit?, include_obsoleted?)` | Keyword search over all RFC titles; returns number, title, status (STD/BCP/INF/HIST/EXP), date, obsoletion |
| `get_rfc(number, section?, include_subsections?)` | Whole RFC text, or one numbered section like `"5"` or `"5.6"` (lazy-fetched and cached) |
| `get_rfc_toc(number)` | Table of contents: every numbered section with its title |
| `search_rfc_content(query, rfc?, limit?)` | FTS keyword search across cached RFCs, returning ranked sections with snippets |
| `list_cached_rfc()` | What is cached locally (index count, sizes, fetch times) |

Typical agent flow: `search_rfc_titles("stateless network protocol")` →
`get_rfc_toc(9110)` → `get_rfc(9110, section="5")`.

## Open WebUI integration (via mcpo)

```bash
pip install mcpo
mcpo --port 8001 -- /path/to/servers/rfc-reference/.venv/bin/python /path/to/servers/rfc-reference/main.py
```

In Open WebUI: **Admin Settings → Functions → Tools API Server** →
`http://localhost:8001`.

## Docker

```bash
docker build -t rfc-reference-mcp .
docker run --rm -p 8001:8001 -v rfc-cache:/data -e RFC_DB=/data/rfc.db rfc-reference-mcp
```

Prime the index inside the container:

```bash
docker exec <container> python /app/sync.py
```

## Notes

- Section detection uses flush-left numbered headings, which skips the
  indented Table of Contents in both classic and modern RFC layouts.
- Page headers/footers (`[...] [Page 12]`) and form feeds are stripped
  from section text.
- Very large documents are truncated at 400 KB in `get_rfc`; prefer
  TOC + section retrieval for those.
- Full-text search only covers RFCs already cached locally; the title
  index always covers every published RFC.

## License

MIT — see repository root.
