# SPDX-License-Identifier: Apache-2.0
from notion_exporter.ir import Export
from notion_exporter.parsers import v2024_md_csv
from notion_exporter.uuid_strategy import normalize
from notion_exporter.zip_loader import open_export

from .conftest import HEX_DB_TASKS, HEX_PAGE_HOME, HEX_PAGE_NOTES


def _parse(zip_path):
    loaded = open_export(str(zip_path))
    export = Export()
    v2024_md_csv.parse(loaded, export)
    loaded.zf.close()
    return export


def test_top_level_page_found(notion_zip):
    export = _parse(notion_zip)
    home = next(p for p in export.pages if p.uuid == normalize(HEX_PAGE_HOME))
    assert home.title == "Home"
    assert home.parent_uuid is None


def test_subpage_parented_to_home(notion_zip):
    export = _parse(notion_zip)
    notes = next(p for p in export.pages if p.uuid == normalize(HEX_PAGE_NOTES))
    assert notes.parent_uuid == normalize(HEX_PAGE_HOME)


def test_home_page_blocks_cover_kinds(notion_zip):
    export = _parse(notion_zip)
    home = next(p for p in export.pages if p.uuid == normalize(HEX_PAGE_HOME))
    kinds = [b.kind for b in home.blocks]
    # leading `# Home` should be skipped (it's the page title)
    assert "heading_2" in kinds  # ## Quick links
    assert "todo" in kinds
    assert "bulleted" in kinds
    assert "quote" in kinds
    assert "code" in kinds
    assert "divider" in kinds


def test_todo_checked_state_parsed(notion_zip):
    export = _parse(notion_zip)
    home = next(p for p in export.pages if p.uuid == normalize(HEX_PAGE_HOME))
    todos = [b for b in home.blocks if b.kind == "todo"]
    assert len(todos) == 2
    assert todos[0].extra["checked"] is False
    assert todos[1].extra["checked"] is True


def test_image_block_in_notes(notion_zip):
    export = _parse(notion_zip)
    notes = next(p for p in export.pages if p.uuid == normalize(HEX_PAGE_NOTES))
    images = [b for b in notes.blocks if b.kind == "image"]
    assert len(images) == 1
    assert images[0].extra["src"] == "screenshot.png"


def test_database_parsed(notion_zip):
    export = _parse(notion_zip)
    assert len(export.databases) == 1
    db = export.databases[0]
    assert db.uuid == normalize(HEX_DB_TASKS)
    assert db.name == "Tasks"
    assert db.table_name == "data_tasks"
    # 4 columns: Name, Status, Priority, Done
    cols = {c.name: c for c in db.columns}
    assert set(cols) == {"Name", "Status", "Priority", "Done"}
    # Priority should be INTEGER (values 3, 1)
    assert cols["Priority"].sqlite_type == "INTEGER"
    # Done should be checkbox-shaped (Yes/No)
    assert cols["Done"].sqlite_type == "INTEGER"
    assert cols["Done"].notion_type == "checkbox"


def test_prefers_all_csv_over_view_csv(notion_zip):
    export = _parse(notion_zip)
    db = export.databases[0]
    # `_all.csv` has 2 rows; view-only `.csv` has 1 — we must pick _all
    assert len(db.rows) == 2


def test_row_bodies_attached(notion_zip):
    export = _parse(notion_zip)
    db = export.databases[0]
    rows_with_blocks = [r for r in db.rows if r.blocks]
    assert len(rows_with_blocks) == 2
    # Each row body has at least one paragraph
    for r in rows_with_blocks:
        kinds = [b.kind for b in r.blocks]
        assert "paragraph" in kinds


def test_attachment_collected(notion_zip):
    export = _parse(notion_zip)
    assert len(export.attachments) == 1
    att = export.attachments[0]
    assert att.original_path.endswith("screenshot.png")
    assert att.mime == "image/png"
    assert att.size_bytes > 0
    assert len(att.sha256) == 64
