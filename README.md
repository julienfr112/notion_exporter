# notion-exporter

Convert a Notion `.zip` export into a portable SQLite database (+ sidecar attachments directory). The output is consumable by [Dispatch](https://dispatch.ovh) and by any other project that wants Notion content in a clean, queryable shape — no Notion API, no rate limits, no auth.

> **Status: alpha, single-consumer.** The first consumer is [Dispatch](https://dispatch.ovh), which embeds this repository as a git submodule and runs the CLI against Notion exports. Apache-2.0 licensed; PRs and issues welcome.

## Install

```sh
uv sync
```

## Usage

```sh
uv run notion-exporter convert path/to/notion-export.zip out.sqlite
```

…or, equivalently, `uv run python -m notion_exporter convert …`.

Output:

- `out.sqlite` — clean queryable database (schema below).
- `out.attachments/` — images, PDFs, and other file uploads from the export, named by content hash.

Pass `--embed-attachments` to store binary attachments as BLOBs inside the SQLite file instead (single-file mode, recommended only for small workspaces — Notion exports routinely include 100MB+ of media).

## Output schema

The export is a regular SQLite database. Open it with any SQLite client.

### `kv` — block tree

Every page and every block (paragraph, heading, todo, callout, image, …) is a row. The block tree is encoded via `parent`.

| column | type | notes |
|---|---|---|
| `uuid` | TEXT PK | hyphenated UUID. Pages use Notion's own UUID; sub-page blocks use `uuid5(NAMESPACE_DISPATCH, "{page_uuid}/{path}/{content_hash}")` for deterministic re-imports. |
| `parent` | TEXT | parent uuid, NULL for workspace roots |
| `kind` | TEXT | `page`, `paragraph`, `heading_1..3`, `todo`, `bulleted`, `numbered`, `quote`, `callout`, `toggle`, `code`, `image`, `file`, `bookmark`, `page_link`, `table`, `database`, `db_row`, `divider` |
| `pos` | INTEGER | ordering within parent |
| `title` | TEXT | hoisted for `kind='page'` and `kind='database'` |
| `text` | TEXT | hoisted plain-text concat for text-bearing blocks, FTS-friendly |
| `page_uuid` | TEXT | denormalized ancestor page for `WHERE page_uuid = ?` queries without recursive CTE |
| `json` | TEXT | full payload as JSON. Schema varies by `kind` — see `notion_exporter/parsers/v2024_md_csv.py` for the per-kind shapes. |

Common `extra` fields on `kv.json`:
- `image` / `file` / `bookmark` / `page_link`: `src` or `href` (original markdown URL), `alt`, `attachment_uuid` (set when the relative URL resolves to a row in `attachment`), `target_page_uuid` (set when the URL points at another exported page).
- `paragraph`: `links` — list of `{label, href, target_page_uuid?, attachment_uuid?}` recovered from inline `[label](href)` syntax.
- `toggle` / `callout`: `title` carries the summary text; children blocks appear as separate `kv` rows with `parent = <container.uuid>`.
- `table`: `headers` (list[str]) + `rows` (list[list[str]]).
- `todo`: `checked` (bool).
- `code`: `language`.

Indexed on `(parent)` and `(kind, page_uuid)`.

### `notion_database` — database metadata

One row per Notion database. Each row names the matching `data_*` table that holds the actual rows.

| column | type | notes |
|---|---|---|
| `uuid` | TEXT PK | same as the `kind='database'` row in `kv` (enforced via `FOREIGN KEY`) |
| `name` | TEXT | human-readable database name |
| `table_name` | TEXT | name of the matching `data_*` table. Two Notion databases with the same name get distinct tables (the second gets a `_<uuid8>` suffix). |
| `schema_json` | TEXT | per-column `name`, `sqlite_type`, `notion_type` (`text`, `number`, `checkbox`, `select`, `multi_select`) — JSON |

### `data_<sanitized_name>` — per-database row tables

One table per Notion database, columns inferred from the CSV. Type inference: numbers → INTEGER/REAL, ISO dates → TEXT (kept as text — consumers parse), checkboxes → INTEGER 0/1, select/multi-select → TEXT. Every table has a `uuid TEXT PRIMARY KEY`.

### `attachment` — files

| column | type | notes |
|---|---|---|
| `uuid` | TEXT PK | derived: `uuid5(NAMESPACE_DISPATCH, sha256_hex)` |
| `original_path` | TEXT | path inside the source zip |
| `rel_path` | TEXT | path relative to `<output>.attachments/` (sidecar mode) |
| `mime` | TEXT | guessed from extension |
| `sha256` | TEXT | hex |
| `size_bytes` | INTEGER | |
| `blob` | BLOB | populated only with `--embed-attachments` |

### `export_meta` — provenance

Key/value: `exporter_version`, `parser_version`, `exported_at`, `source_zip_sha256`.

## Idempotency

Running the exporter twice on the same `.zip` produces identical UUIDs in the `kv` table — by design. This lets Dispatch and other consumers do diff-imports rather than full reloads when a workspace is re-exported.

Caveat: sub-page block UUIDs are derived from `(page_uuid, position, content_hash)`. **Editing a paragraph rotates its UUID.** That matches what users intuitively expect from a re-import and works fine with Dispatch's versioned KV store.

## Inspect the output

```sh
sqlite3 out.sqlite ".schema"
sqlite3 out.sqlite "SELECT kind, COUNT(*) FROM kv GROUP BY kind"
sqlite3 out.sqlite "SELECT json_extract(json, '\$.title') FROM kv WHERE kind='page' LIMIT 10"
sqlite3 out.sqlite "SELECT name, table_name FROM notion_database"
```

## Limitations (v1)

- **Notion "Markdown & CSV" export format only.** HTML exports are not supported.
- **Formula columns** are stored as their last-evaluated string (this is what Notion writes to CSV; we don't re-evaluate).
- **Relations** are recovered from the per-row `.md` files when present; CSV alone flattens them to text.
- **No two-way sync.** Read-only.

## Notion format versioning

Notion has changed its export zip layout repeatedly (notable shifts in 2022 and 2023). The parser is versioned: `notion_exporter/parsers/v2024_md_csv.py`. `zip_loader.py` detects the format on entry and fails loudly on unknown layouts — better than silent corruption.

## License

Apache 2.0 — see [LICENSE](LICENSE).
