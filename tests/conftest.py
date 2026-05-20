# SPDX-License-Identifier: Apache-2.0
"""Builds a small Notion-shaped export zip on the fly.

Committing a binary fixture would make changes invisible in diffs; building it
in code keeps the structure explicit and reviewable.
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

# Stable hex UUIDs used in the fixture (32 hex chars, no hyphens — Notion's form)
HEX_PAGE_HOME = "11111111111111111111111111111111"
HEX_PAGE_NOTES = "22222222222222222222222222222222"
HEX_DB_TASKS = "33333333333333333333333333333333"
HEX_ROW_TASK_A = "44444444444444444444444444444444"
HEX_ROW_TASK_B = "55555555555555555555555555555555"


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
