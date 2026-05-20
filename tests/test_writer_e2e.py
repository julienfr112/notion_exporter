# SPDX-License-Identifier: Apache-2.0
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from notion_exporter.cli import main

from .conftest import HEX_DB_TASKS, HEX_PAGE_HOME, HEX_PAGE_NOTES
from notion_exporter.uuid_strategy import normalize


def _run(notion_zip: Path, tmp_path: Path, embed: bool = False) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out.sqlite"
    args = ["convert", str(notion_zip), str(out), "--quiet"]
    if embed:
        args.append("--embed-attachments")
    rc = main(args)
    assert rc == 0
    return out


def _conn(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c


def test_schema_applied(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path)
    c = _conn(out)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"kv", "notion_database", "attachment", "export_meta", "data_tasks"} <= tables


def test_kv_contains_pages_and_blocks(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path)
    c = _conn(out)
    counts = {
        kind: cnt
        for kind, cnt in c.execute("SELECT kind, COUNT(*) FROM kv GROUP BY kind")
    }
    # 2 pages: Home + Notes
    assert counts["page"] == 2
    # 1 database
    assert counts["database"] == 1
    # 2 db rows
    assert counts["db_row"] == 2
    # blocks are present
    assert counts.get("paragraph", 0) >= 1
    assert counts.get("todo", 0) == 2


def test_kv_uuids_match_normalized_hex(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path)
    c = _conn(out)
    home = c.execute(
        "SELECT uuid, title, parent FROM kv WHERE kind='page' AND title='Home'"
    ).fetchone()
    assert home["uuid"] == normalize(HEX_PAGE_HOME)
    assert home["parent"] is None
    notes = c.execute(
        "SELECT uuid, title, parent FROM kv WHERE kind='page' AND title='Notes'"
    ).fetchone()
    assert notes["uuid"] == normalize(HEX_PAGE_NOTES)
    assert notes["parent"] == normalize(HEX_PAGE_HOME)


def test_data_tasks_typed_columns(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path)
    c = _conn(out)
    info = list(c.execute("PRAGMA table_info(data_tasks)"))
    cols = {r["name"]: r["type"] for r in info}
    assert cols["uuid"] == "TEXT"
    assert cols["Priority"] == "INTEGER"
    assert cols["Done"] == "INTEGER"
    # Two rows, Done is 0/1 not Yes/No
    rows = list(c.execute("SELECT Name, Priority, Done FROM data_tasks ORDER BY Name"))
    assert rows[0]["Name"] == "Task A"
    assert rows[0]["Priority"] == 3
    assert rows[0]["Done"] == 0
    assert rows[1]["Name"] == "Task B"
    assert rows[1]["Done"] == 1


def test_notion_database_metadata(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path)
    c = _conn(out)
    row = c.execute("SELECT uuid, name, table_name, schema_json FROM notion_database").fetchone()
    assert row["uuid"] == normalize(HEX_DB_TASKS)
    assert row["name"] == "Tasks"
    assert row["table_name"] == "data_tasks"
    schema = json.loads(row["schema_json"])
    assert {col["name"] for col in schema["columns"]} == {"Name", "Status", "Priority", "Done"}


def test_attachment_sidecar_written(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path)
    c = _conn(out)
    row = c.execute("SELECT rel_path, sha256, size_bytes, blob FROM attachment").fetchone()
    assert row["blob"] is None
    sidecar = tmp_path / "out.sqlite.attachments" / row["rel_path"]
    assert sidecar.exists()
    assert sidecar.stat().st_size == row["size_bytes"]


def test_attachment_embed_mode(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path, embed=True)
    c = _conn(out)
    row = c.execute("SELECT blob, size_bytes FROM attachment").fetchone()
    assert row["blob"] is not None
    assert len(row["blob"]) == row["size_bytes"]


def test_export_meta_populated(notion_zip, tmp_path):
    out = _run(notion_zip, tmp_path)
    c = _conn(out)
    meta = {k: v for k, v in c.execute("SELECT key, value FROM export_meta")}
    assert meta["parser_version"] == "v2024_md_csv"
    assert "exporter_version" in meta
    assert len(meta["source_zip_sha256"]) == 64


def test_idempotent_uuids_across_runs(notion_zip, tmp_path):
    out1 = _run(notion_zip, tmp_path / "a")
    out2 = _run(notion_zip, tmp_path / "b")
    c1, c2 = _conn(out1), _conn(out2)
    uuids1 = sorted(r[0] for r in c1.execute("SELECT uuid FROM kv"))
    uuids2 = sorted(r[0] for r in c2.execute("SELECT uuid FROM kv"))
    assert uuids1 == uuids2
