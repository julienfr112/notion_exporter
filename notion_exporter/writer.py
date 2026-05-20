# SPDX-License-Identifier: Apache-2.0
"""Turn the IR into a SQLite database + sidecar attachments directory.

The writer is intentionally separate from the parser so the SQLite shape can
evolve (or be replaced by a different format) without touching parser logic.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import sqlite3
from importlib import resources
from pathlib import Path

from . import __version__
from .ir import Attachment, Block, Column, Database, Export, Page, Row


def write(
    export: Export,
    out_sqlite: str,
    *,
    attachments_dir: str | None,
    embed_attachments: bool,
    parser_version: str,
    source_zip_path: str,
) -> None:
    """Open `out_sqlite`, apply schema, write all IR rows + attachments."""
    if os.path.exists(out_sqlite):
        os.remove(out_sqlite)

    conn = sqlite3.connect(out_sqlite)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _apply_schema(conn)
        with conn:
            for page in export.pages:
                _insert_page(conn, page)
            for db in export.databases:
                _insert_database(conn, db)
            for att in export.attachments:
                _insert_attachment(
                    conn,
                    att,
                    attachments_dir=attachments_dir,
                    embed=embed_attachments,
                )
            _write_meta(
                conn,
                parser_version=parser_version,
                source_zip_path=source_zip_path,
            )
    finally:
        conn.close()


def _apply_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("notion_exporter").joinpath("schema.sql").read_text()
    conn.executescript(sql)


# ── pages + blocks ──


def _insert_page(conn: sqlite3.Connection, page: Page) -> None:
    payload = {
        "title": page.title,
        "parent": page.parent_uuid,
    }
    conn.execute(
        "INSERT OR REPLACE INTO kv "
        "(uuid, parent, kind, pos, title, text, page_uuid, json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            page.uuid,
            page.parent_uuid,
            "page",
            page.pos,
            page.title,
            None,
            page.uuid,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    for b in page.blocks:
        _insert_block(conn, b)


def _insert_block(conn: sqlite3.Connection, b: Block) -> None:
    payload = {"text": b.text}
    if b.title is not None:
        payload["title"] = b.title
    if b.extra:
        payload["extra"] = b.extra
    conn.execute(
        "INSERT OR REPLACE INTO kv "
        "(uuid, parent, kind, pos, title, text, page_uuid, json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            b.uuid,
            b.parent_uuid,
            b.kind,
            b.pos,
            b.title,
            b.text,
            b.page_uuid,
            json.dumps(payload, ensure_ascii=False),
        ),
    )


# ── databases ──


def _insert_database(conn: sqlite3.Connection, db: Database) -> None:
    schema_json = json.dumps(
        {
            "columns": [
                {
                    "name": c.name,
                    "sqlite_type": c.sqlite_type,
                    "notion_type": c.notion_type,
                }
                for c in db.columns
            ]
        },
        ensure_ascii=False,
    )
    conn.execute(
        "INSERT OR REPLACE INTO kv "
        "(uuid, parent, kind, pos, title, text, page_uuid, json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            db.uuid,
            db.parent_uuid,
            "database",
            db.pos,
            db.name,
            None,
            db.uuid,
            json.dumps({"name": db.name, "table_name": db.table_name}, ensure_ascii=False),
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO notion_database (uuid, name, table_name, schema_json) "
        "VALUES (?,?,?,?)",
        (db.uuid, db.name, db.table_name, schema_json),
    )
    _create_data_table(conn, db)
    for idx, row in enumerate(db.rows):
        _insert_row(conn, db, row, idx)


def _create_data_table(conn: sqlite3.Connection, db: Database) -> None:
    # Build column DDL — sanitize column names for SQLite identifiers
    col_defs = ['"uuid" TEXT PRIMARY KEY']
    for c in db.columns:
        col_defs.append(f'"{_quote_ident(c.name)}" {c.sqlite_type}')
    conn.execute(f'CREATE TABLE IF NOT EXISTS "{db.table_name}" ({", ".join(col_defs)})')


def _insert_row(conn: sqlite3.Connection, db: Database, row: Row, idx: int) -> None:
    # Row in the typed data_* table
    cols = ['"uuid"'] + [f'"{_quote_ident(c.name)}"' for c in db.columns]
    placeholders = ["?"] * len(cols)
    values: list[object] = [row.uuid] + [row.values.get(c.name) for c in db.columns]
    conn.execute(
        f'INSERT OR REPLACE INTO "{db.table_name}" '
        f'({", ".join(cols)}) VALUES ({", ".join(placeholders)})',
        values,
    )
    # Mirror as a kv node so consumers can navigate db rows via the kv tree
    first_col = db.columns[0].name if db.columns else None
    title = str(row.values.get(first_col, "")) if first_col else None
    conn.execute(
        "INSERT OR REPLACE INTO kv "
        "(uuid, parent, kind, pos, title, text, page_uuid, json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            row.uuid,
            db.uuid,
            "db_row",
            idx,
            title,
            None,
            row.uuid,
            json.dumps({"values": row.values}, ensure_ascii=False, default=str),
        ),
    )
    for b in row.blocks:
        _insert_block(conn, b)


# ── attachments ──


def _insert_attachment(
    conn: sqlite3.Connection,
    att: Attachment,
    *,
    attachments_dir: str | None,
    embed: bool,
) -> None:
    blob: bytes | None = None
    if embed:
        blob = att.data
    else:
        if attachments_dir is None:
            raise ValueError("attachments_dir required when embed=False")
        target = Path(attachments_dir) / att.rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(att.data)
    conn.execute(
        "INSERT OR REPLACE INTO attachment "
        "(uuid, original_path, rel_path, mime, sha256, size_bytes, blob) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            att.uuid,
            att.original_path,
            att.rel_path,
            att.mime,
            att.sha256,
            att.size_bytes,
            blob,
        ),
    )


# ── meta ──


def _write_meta(
    conn: sqlite3.Connection,
    *,
    parser_version: str,
    source_zip_path: str,
) -> None:
    with open(source_zip_path, "rb") as f:
        zip_sha = hashlib.sha256(f.read()).hexdigest()
    rows = {
        "exporter_version": __version__,
        "parser_version": parser_version,
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "source_zip_sha256": zip_sha,
    }
    for k, v in rows.items():
        conn.execute(
            "INSERT OR REPLACE INTO export_meta (key, value) VALUES (?,?)",
            (k, v),
        )


def _quote_ident(name: str) -> str:
    # Escape embedded double-quotes; outer quoting is done by caller.
    return name.replace('"', '""')
