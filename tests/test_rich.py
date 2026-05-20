# SPDX-License-Identifier: Apache-2.0
"""Tests for the enhanced parser: link resolution, toggles, callouts, tables,
multi-select detection, name-collision disambiguation."""
import sqlite3
from pathlib import Path

from notion_exporter.cli import main
from notion_exporter.ir import Export
from notion_exporter.parsers import v2024_md_csv
from notion_exporter.uuid_strategy import normalize
from notion_exporter.zip_loader import open_export

from .conftest import HEX_RICH_DB1, HEX_RICH_DB2, HEX_RICH_INDEX, HEX_RICH_REF


def _parse(zip_path: Path) -> Export:
    loaded = open_export(str(zip_path))
    export = Export()
    v2024_md_csv.parse(loaded, export)
    loaded.zf.close()
    return export


def _run_cli(zip_path: Path, tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out.sqlite"
    assert main(["convert", str(zip_path), str(out), "--quiet"]) == 0
    return out


def _conn(p: Path) -> sqlite3.Connection:
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    return c


# ── markdown richness ──


def test_callout_recognized(rich_notion_zip):
    export = _parse(rich_notion_zip)
    index = next(p for p in export.pages if p.uuid == normalize(HEX_RICH_INDEX))
    kinds = [b.kind for b in index.blocks]
    assert "callout" in kinds


def test_toggle_recognized_with_summary_and_children(rich_notion_zip):
    export = _parse(rich_notion_zip)
    index = next(p for p in export.pages if p.uuid == normalize(HEX_RICH_INDEX))
    toggle = next(b for b in index.blocks if b.kind == "toggle")
    assert toggle.text == "Click to expand"
    # The toggle's children are flat blocks in the page with parent=toggle.uuid
    children = [b for b in index.blocks if b.parent_uuid == toggle.uuid]
    assert len(children) >= 2
    kinds = {b.kind for b in children}
    assert "paragraph" in kinds
    assert "bulleted" in kinds


def test_inline_table_parsed(rich_notion_zip):
    export = _parse(rich_notion_zip)
    index = next(p for p in export.pages if p.uuid == normalize(HEX_RICH_INDEX))
    table = next(b for b in index.blocks if b.kind == "table")
    assert table.extra["headers"] == ["Col A", "Col B"]
    assert table.extra["rows"] == [["a1", "b1"], ["a2", "b2"]]


# ── link resolution ──


def test_inline_image_resolved_to_attachment_uuid(rich_notion_zip):
    export = _parse(rich_notion_zip)
    index = next(p for p in export.pages if p.uuid == normalize(HEX_RICH_INDEX))
    image = next(b for b in index.blocks if b.kind == "image")
    assert "attachment_uuid" in image.extra
    att = next(a for a in export.attachments if a.uuid == image.extra["attachment_uuid"])
    assert att.original_path.endswith("photo.png")


def test_page_mention_inline_resolved(rich_notion_zip):
    export = _parse(rich_notion_zip)
    index = next(p for p in export.pages if p.uuid == normalize(HEX_RICH_INDEX))
    para = next(
        b for b in index.blocks
        if b.kind == "paragraph" and "Ref" in (b.text or "")
    )
    links = para.extra.get("links", [])
    assert any(l.get("target_page_uuid") == normalize(HEX_RICH_REF) for l in links)


def test_standalone_file_link_becomes_file_block(rich_notion_zip):
    export = _parse(rich_notion_zip)
    index = next(p for p in export.pages if p.uuid == normalize(HEX_RICH_INDEX))
    files = [b for b in index.blocks if b.kind == "file"]
    assert len(files) == 1
    assert files[0].text == "Download spec"
    assert "attachment_uuid" in files[0].extra


def test_url_percent_decoded_during_resolution(rich_notion_zip):
    # The inline image href was `Ref%20<hex>/photo.png`. If we didn't decode
    # before lookup, the resolution would fail and no attachment_uuid would be
    # attached. test_inline_image_resolved_to_attachment_uuid already covers
    # the success path; this is an explicit guard against regressions.
    export = _parse(rich_notion_zip)
    images = [b for p in export.pages for b in p.blocks if b.kind == "image"]
    assert all("attachment_uuid" in img.extra for img in images), (
        "every inline image should resolve to a known attachment"
    )


# ── CSV correctness ──


def test_multi_select_column_detected(rich_notion_zip):
    export = _parse(rich_notion_zip)
    # The first Reports DB has a Tags column with comma-separated values
    db = next(d for d in export.databases if d.uuid == normalize(HEX_RICH_DB1))
    tags = next(c for c in db.columns if c.name == "Tags")
    assert tags.notion_type == "multi_select"


def test_duplicate_column_names_disambiguated(rich_notion_zip):
    export = _parse(rich_notion_zip)
    # The first Reports DB CSV has two "Tags" columns
    db = next(d for d in export.databases if d.uuid == normalize(HEX_RICH_DB1))
    names = [c.name for c in db.columns]
    assert names.count("Tags") == 1, "duplicate columns must be renamed"
    assert "Tags_2" in names


def test_two_dbs_same_name_get_distinct_tables(rich_notion_zip, tmp_path):
    out = _run_cli(rich_notion_zip, tmp_path)
    c = _conn(out)
    rows = list(c.execute("SELECT uuid, name, table_name FROM notion_database"))
    assert len(rows) == 2
    tables = {r["table_name"] for r in rows}
    assert len(tables) == 2  # distinct
    # Both physical tables actually exist
    for tn in tables:
        cnt = c.execute(f"SELECT COUNT(*) FROM \"{tn}\"").fetchone()[0]
        assert cnt > 0


# ── writer / schema ──


def test_fk_from_notion_database_to_kv(rich_notion_zip, tmp_path):
    out = _run_cli(rich_notion_zip, tmp_path)
    c = _conn(out)
    fks = list(c.execute("PRAGMA foreign_key_list(notion_database)"))
    assert any(fk["table"] == "kv" and fk["to"] == "uuid" for fk in fks)
    # And no violations exist in the produced data
    c.execute("PRAGMA foreign_keys=ON")
    violations = list(c.execute("PRAGMA foreign_key_check"))
    assert violations == []


def test_toggle_children_written_with_parent_link(rich_notion_zip, tmp_path):
    out = _run_cli(rich_notion_zip, tmp_path)
    c = _conn(out)
    toggle_uuid = c.execute(
        "SELECT uuid FROM kv WHERE kind='toggle' LIMIT 1"
    ).fetchone()["uuid"]
    children = list(c.execute("SELECT kind FROM kv WHERE parent = ?", (toggle_uuid,)))
    assert len(children) >= 2


def test_python_dash_m_entry(rich_notion_zip, tmp_path):
    """python -m notion_exporter should work the same as the script entry."""
    import subprocess
    import sys
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "out.sqlite"
    rc = subprocess.run(
        [sys.executable, "-m", "notion_exporter",
         "convert", str(rich_notion_zip), str(out), "--quiet"],
        capture_output=True,
    )
    assert rc.returncode == 0, rc.stderr
    assert out.exists()


# ── nested-block uuid uniqueness ──


def test_nested_block_uuids_unique_from_top_level(rich_notion_zip):
    """Including parent_uuid in derive_block prevents same-pos same-content
    collisions between top-level blocks and nested children."""
    export = _parse(rich_notion_zip)
    uuids = [b.uuid for p in export.pages for b in p.blocks]
    assert len(uuids) == len(set(uuids)), "all block uuids within a page must be unique"
