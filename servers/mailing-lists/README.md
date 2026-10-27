# Mailing Lists MCP Server

An MCP (Model Context Protocol) server that lets LLMs search and read
archived mailing list discussions. A companion CLI utility keeps a local
SQLite database up to date from mbox archives — plain or gzipped, local
files or downloadable URLs. Duplicate messages are silently ignored, so
syncing is idempotent.

## Components

| File | Role |
|---|---|
| `sync.py` | CLI importer: mbox / mbox.gz files and URLs → SQLite |
| `main.py` | MCP server (stdio) with read-only search/browse tools |
| `database.py` | Storage layer: FTS5 full-text index, dedup, thread resolution |

## Setup

```bash
cd servers/mailing-lists
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Requires Python 3.10+ with a sqlite3 that has FTS5 enabled (default in
most builds).

## 1. Sync the archive

Copy `config.yaml.example` to `config.yaml` and point it at your archives:

```yaml
db: ~/.mailing-lists-mcp/lists.db
lists:
  - name: xorg-devel
    url: https://lists.example.org/archives/xorg.mbox.gz   # gzipped mbox over HTTP
  - name: lkml
    path: /srv/archives/lkml                               # dir of *.mbox / *.mbox.gz
```

Then:

```bash
.venv/bin/python sync.py --config config.yaml
```

Or without a config file:

```bash
.venv/bin/python sync.py --list mylist --path /srv/archives/mylist.mbox.gz
.venv/bin/python sync.py --list mylist --url https://lists.example.org/mylist.mbox.gz
```

Behavior:

- **Gzip** is auto-detected by magic bytes; `.mbox` and `.mbox.gz` both work.
- **Duplicates ignored**: messages are deduplicated on `Message-ID` (and a
  content hash for mails lacking one). Re-importing the same archive adds
  nothing.
- **Incremental**: unchanged local files are skipped (mtime/size); URLs are
  re-fetched with `If-None-Match` / `If-Modified-Since` and honored on `304`.
  Use `--force` to re-import regardless.
- **Threads** are resolved from `In-Reply-To` / `References` after each run.

Schedule it in cron to keep the database fresh:

```cron
0 3 * * * /path/to/servers/mailing-lists/.venv/bin/python /path/to/servers/mailing-lists/sync.py --config /path/to/config.yaml
```

## 2. Run the MCP server

The database path comes from `--db` at sync time, or the `MLIST_DB`
environment variable at serve time (default `~/.mailing-lists-mcp/lists.db`).

```bash
MLIST_DB=/path/to/lists.db .venv/bin/python main.py    # stdio transport
```

Direct MCP client config (Claude Desktop, Open WebUI MCP support, etc.):

```json
{
  "mcpServers": {
    "mailing-lists": {
      "command": "/path/to/servers/mailing-lists/.venv/bin/python",
      "args": ["/path/to/servers/mailing-lists/main.py"],
      "env": { "MLIST_DB": "/home/you/.mailing-lists-mcp/lists.db" }
    }
  }
}
```

### Tools

| Tool | Purpose |
|---|---|
| `list_available_lists` | Archived lists with message counts and date ranges |
| `search_discussions(query, list_name?, since?, until?, limit?)` | FTS5 full-text search; returns threads ranked by relevance with snippet, participants, and date range |
| `get_message(message_id)` | Full text of one message by Message-ID |
| `get_thread(message_id)` | Entire discussion chronologically, given any message in it |
| `list_messages(list_name?, since?, until?, author?, subject_contains?, thread_root?, offset?, limit?)` | Browse/filter messages by date, author, subject, or thread |

## 3. Open WebUI integration (via mcpo)

Expose the stdio server as an HTTP tool API with
[mcpo](https://github.com/open-webui/mcpo):

```bash
pip install mcpo
mcpo --port 8000 -- /path/to/servers/mailing-lists/.venv/bin/python /path/to/servers/mailing-lists/main.py
```

In Open WebUI: **Admin Settings → Functions → Tools API Server** →
`http://localhost:8000`.

## Docker

```bash
docker build -t mailing-lists-mcp .
docker run --rm -p 8000:8000 \
  -v mlist-data:/data \
  -v /path/to/archives:/archives:ro \
  -e MLIST_DB=/data/lists.db \
  mailing-lists-mcp
```

Sync inside the container (then the running server sees new mail):

```bash
docker exec <container> python /app/sync.py --list mylist --path /archives/mylist.mbox.gz
```

## License

MIT — see repository root.
