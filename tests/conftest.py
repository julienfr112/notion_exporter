# SPDX-License-Identifier: Apache-2.0
"""Builds Notion-shaped export zips on the fly.

Committing a binary fixture would make changes invisible in diffs; building it
in code keeps the structure explicit and reviewable.

Two fixtures:
- `notion_zip`: minimal but representative — basic page/db/attachment shapes.
- `rich_notion_zip`: exercises link resolution, toggles, callouts, tables,
  multi-select, name-collision disambiguation.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

# Stable hex UUIDs used in the fixture (32 hex chars, no hyphens — Notion's form)
HEX_PAGE_HOME = "11111111111111111111111111111111"
HEX_PAGE_NOTES = "22222222222222222222222222222222"
HEX_DB_TASKS = "33333333333333333333333333333333"
HEX_ROW_TASK_A = "44444444444444444444444444444444"
HEX_ROW_TASK_B = "55555555555555555555555555555555"

# Rich-fixture UUIDs
HEX_RICH_INDEX = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEX_RICH_REF = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
HEX_RICH_DB1 = "cccccccccccccccccccccccccccccccc"
HEX_RICH_DB2 = "dddddddddddddddddddddddddddddddd"


def _png_bytes() -> bytes:
    """Minimal 1×1 transparent PNG."""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        + b"\x00\x00\x00\rIDATx\x9cc\xfc\xcf\xc0\xc0\xc0\x00\x00\x00\x05\x00\x01\xa5\xf6E@"
        + b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )


@pytest.fixture
def notion_zip(tmp_path: Path) -> Path:
    """A small but representative Notion 'Markdown & CSV' export.

    Layout:
      Export-test/
        Home <hex>.md                    # top-level page
        Home <hex>/
          Notes <hex>.md                 # sub-page
          Notes <hex>/
            screenshot.png               # attachment
          Tasks <hex>.csv                # database (per-view)
          Tasks <hex>_all.csv            # database (all-rows variant — preferred)
          Tasks <hex>/
            Task A <hex>.md              # row body
            Task B <hex>.md              # row body
    """
    zip_path = tmp_path / "notion-export.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        home_md = (
            f"# Home\n"
            f"\n"
            f"This is the home page.\n"
            f"\n"
            f"## Quick links\n"
            f"\n"
            f"- [ ] First todo\n"
            f"- [x] Second todo, already done\n"
            f"- regular bullet\n"
            f"\n"
            f"> A short quote.\n"
            f"\n"
            f"```python\n"
            f"print('hello')\n"
            f"```\n"
            f"\n"
            f"---\n"
        )
        zf.writestr(f"Export-test/Home {HEX_PAGE_HOME}.md", home_md)

        notes_md = (
            f"# Notes\n"
            f"\n"
            f"A paragraph in the notes page.\n"
            f"\n"
            f"![alt text](screenshot.png)\n"
        )
        zf.writestr(f"Export-test/Home {HEX_PAGE_HOME}/Notes {HEX_PAGE_NOTES}.md", notes_md)
        zf.writestr(
            f"Export-test/Home {HEX_PAGE_HOME}/Notes {HEX_PAGE_NOTES}/screenshot.png",
            _png_bytes(),
        )

        tasks_csv_all = (
            "Name,Status,Priority,Done\n"
            "Task A,In progress,3,No\n"
            "Task B,Done,1,Yes\n"
        )
        zf.writestr(
            f"Export-test/Home {HEX_PAGE_HOME}/Tasks {HEX_DB_TASKS}_all.csv",
            tasks_csv_all,
        )
        # A view-specific .csv that should be IGNORED in favor of _all
        zf.writestr(
            f"Export-test/Home {HEX_PAGE_HOME}/Tasks {HEX_DB_TASKS}.csv",
            "Name,Status\nTask A,In progress\n",
        )
        task_a_md = "# Task A\n\nDetails about task A.\n"
        task_b_md = "# Task B\n\nDetails about task B.\n"
        zf.writestr(
            f"Export-test/Home {HEX_PAGE_HOME}/Tasks {HEX_DB_TASKS}/Task A {HEX_ROW_TASK_A}.md",
            task_a_md,
        )
        zf.writestr(
            f"Export-test/Home {HEX_PAGE_HOME}/Tasks {HEX_DB_TASKS}/Task B {HEX_ROW_TASK_B}.md",
            task_b_md,
        )
    return zip_path


@pytest.fixture
def rich_notion_zip(tmp_path: Path) -> Path:
    """Richer fixture exercising less-common Notion shapes.

    Layout:
      Index <hex>.md                  # page with toggle, callout, table,
                                      # inline image, page link to Ref
      Index <hex>/
        Ref <hex>.md                  # second page (page-link target)
        Ref <hex>/
          photo.png                   # attachment referenced inline by Index
        spec.pdf                      # file referenced as standalone link
        Reports <hex>.csv             # database #1 named "Reports"
        Reports <hex2>.csv            # database #2 also named "Reports"
                                      # — must get a uuid-suffixed table name
    """
    zip_path = tmp_path / "rich-export.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        index_md = (
            f"# Index\n"
            f"\n"
            f"Welcome to the index. See [Ref](Ref%20{HEX_RICH_REF}.md) for details.\n"
            f"\n"
            f"![A photo](Ref%20{HEX_RICH_REF}/photo.png)\n"
            f"\n"
            f"[Download spec](spec.pdf)\n"
            f"\n"
            f"<aside>\n"
            f"💡 Heads up: this is a callout block.\n"
            f"</aside>\n"
            f"\n"
            f"<details>\n"
            f"<summary>Click to expand</summary>\n"
            f"\n"
            f"First nested paragraph inside the toggle.\n"
            f"\n"
            f"- nested bullet\n"
            f"</details>\n"
            f"\n"
            f"| Col A | Col B |\n"
            f"| --- | --- |\n"
            f"| a1 | b1 |\n"
            f"| a2 | b2 |\n"
        )
        zf.writestr(f"Index {HEX_RICH_INDEX}.md", index_md)
        zf.writestr(
            f"Index {HEX_RICH_INDEX}/Ref {HEX_RICH_REF}.md",
            "# Ref\n\nReference page contents.\n",
        )
        zf.writestr(
            f"Index {HEX_RICH_INDEX}/Ref {HEX_RICH_REF}/photo.png",
            _png_bytes(),
        )
        zf.writestr(
            f"Index {HEX_RICH_INDEX}/spec.pdf",
            b"%PDF-1.4\n%fake pdf body\n%%EOF\n",
        )
        # Two databases sharing the same name — must get disambiguated tables.
        # First Reports DB: multi-select column with proper CSV quoting + a
        # duplicate column name to exercise the dedup path.
        zf.writestr(
            f"Index {HEX_RICH_INDEX}/Reports {HEX_RICH_DB1}_all.csv",
            'Title,Tags,Tags\nQ1,"a, b",x\nQ2,"a, b, c",y\n',
        )
        zf.writestr(
            f"Index {HEX_RICH_INDEX}/Reports {HEX_RICH_DB2}_all.csv",
            "Title,Tags\nFoo,one\nBar,two\n",
        )
    return zip_path
