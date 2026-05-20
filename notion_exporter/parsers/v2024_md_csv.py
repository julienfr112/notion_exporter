# SPDX-License-Identifier: Apache-2.0
"""Parser for Notion's 2024 'Markdown & CSV' export format.

Structure: every page is a `<Title> <32hex>.md` file at some path. If the page
has children (sub-pages, sub-databases, attachments), they live in a sibling
directory `<Title> <32hex>/`. Databases are `<Title> <32hex>.csv` files (Notion
emits a `_all` variant alongside the per-view file; we prefer `_all`). Per-row
page content lives in `<DBTitle> <32hex>/<RowTitle> <32hex>.md`.

The parser walks the zip directory tree, producing IR objects. The writer
turns the IR into SQLite — keep parser logic free of SQLite concerns.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import mimetypes
import re
import zipfile
from pathlib import PurePosixPath

from ..ir import Attachment, Block, Column, Database, Export, Page, Row
from ..uuid_strategy import (
    derive_attachment,
    derive_block,
    derive_row,
    extract_from_filename,
)

PARSER_VERSION = "v2024_md_csv"

_HEX_SUFFIX = re.compile(r"\s+[0-9a-fA-F]{32}$")


def parse(loaded, export: Export) -> None:
    zf = loaded.zf
    root = _detect_root_prefix(zf.namelist())
    _walk(zf, root, parent_uuid=None, ancestor_page_uuid=None, export=export, pos=[0])


# ── directory walk ─────────────────────────────────────────────────────────


def _detect_root_prefix(names: list[str]) -> str:
    """Notion sometimes wraps everything in a single `Export-<id>/` folder."""
    real = [n for n in names if n and not n.endswith("/")]
    if not real:
        return ""
    first = real[0].split("/")[0]
    candidate = first + "/"
    if all(n.startswith(candidate) for n in real):
        return candidate
    return ""


def _list_dir(zf: zipfile.ZipFile, base: str) -> tuple[list[str], set[str]]:
    """Return (files, subdir-basenames) directly under `base`."""
    files: list[str] = []
    dirs: set[str] = set()
    blen = len(base)
    for n in zf.namelist():
        if not n.startswith(base) or n == base or n.endswith("/"):
            continue
        rest = n[blen:]
        if "/" in rest:
            dirs.add(rest.split("/", 1)[0])
        else:
            files.append(n)
    return files, dirs


def _walk(
    zf: zipfile.ZipFile,
    base: str,
    parent_uuid: str | None,
    ancestor_page_uuid: str | None,
    export: Export,
    pos: list[int],
) -> None:
    files, dirs = _list_dir(zf, base)
    md_files = [f for f in files if f.endswith(".md")]
    csv_files = _dedupe_csvs([f for f in files if f.endswith(".csv")])
    other_files = [f for f in files if not f.endswith((".md", ".csv"))]

    consumed: set[str] = set()

    for md in sorted(md_files):
        basename = PurePosixPath(md).stem
        page_uuid = extract_from_filename(basename)
        if page_uuid is None:
            # README or workspace header without a hex id — skip
            continue
        title = _strip_hex_suffix(basename)
        p = pos[0]
        pos[0] += 1
        page = Page(uuid=page_uuid, title=title, parent_uuid=parent_uuid, pos=p)
        page.blocks = _parse_markdown(
            zf.read(md).decode("utf-8", errors="replace"),
            page_uuid=page_uuid,
            parent_uuid=page_uuid,
        )
        export.pages.append(page)
        if basename in dirs:
            consumed.add(basename)
            _walk(
                zf,
                base + basename + "/",
                parent_uuid=page_uuid,
                ancestor_page_uuid=page_uuid,
                export=export,
                pos=[0],
            )

    for csv_path in sorted(csv_files):
        basename = PurePosixPath(csv_path).stem
        # The companion dir uses the basename WITHOUT _all.
        dir_basename = basename.removesuffix("_all")
        db_uuid = extract_from_filename(dir_basename)
        if db_uuid is None:
            continue
        name = _strip_hex_suffix(dir_basename)
        table_name = "data_" + _sanitize_table_name(name)
        p = pos[0]
        pos[0] += 1
        db = Database(
            uuid=db_uuid,
            name=name,
            parent_uuid=parent_uuid,
            pos=p,
            table_name=table_name,
        )
        cols, rows = _parse_csv(zf.read(csv_path), db_uuid=db_uuid)
        db.columns = cols
        db.rows = rows
        # Row bodies & sub-row attachments live in the companion dir.
        if dir_basename in dirs:
            consumed.add(dir_basename)
            _attach_row_contents(
                zf,
                base + dir_basename + "/",
                db=db,
                export=export,
            )
        export.databases.append(db)

    for f in sorted(other_files):
        _add_attachment(zf, f, parent_uuid=ancestor_page_uuid, export=export)

    # Orphan dirs — recurse so we don't lose nested content
    for d in sorted(dirs - consumed):
        _walk(
            zf,
            base + d + "/",
            parent_uuid=parent_uuid,
            ancestor_page_uuid=ancestor_page_uuid,
            export=export,
            pos=[0],
        )


def _attach_row_contents(
    zf: zipfile.ZipFile,
    base: str,
    db: Database,
    export: Export,
) -> None:
    """For each row .md in the database's companion dir, find the matching IR Row
    by title and attach its parsed blocks. Files without a row match become attachments.
    """
    files, dirs = _list_dir(zf, base)
    rows_by_uuid = {r.uuid: r for r in db.rows}
    consumed: set[str] = set()
    for md in sorted(f for f in files if f.endswith(".md")):
        basename = PurePosixPath(md).stem
        row_hex_uuid = extract_from_filename(basename)
        if row_hex_uuid is None:
            continue
        # Map back to a Row: the CSV-derived rows used derive_row(db_uuid, row_index).
        # Notion's per-row md uses Notion's row id. We match by row title (Notion's
        # first column always == row title for db rows).
        title = _strip_hex_suffix(basename)
        matched = next(
            (r for r in db.rows if _row_title(r, db) == title),
            None,
        )
        if matched is None:
            # Create a synthetic row keyed by Notion's hex id
            synthetic_uuid = derive_row(db.uuid, row_hex_uuid)
            matched = Row(uuid=synthetic_uuid, values={"name": title})
            db.rows.append(matched)
            rows_by_uuid[matched.uuid] = matched
        matched.blocks = _parse_markdown(
            zf.read(md).decode("utf-8", errors="replace"),
            page_uuid=matched.uuid,
            parent_uuid=matched.uuid,
        )
        if basename in dirs:
            consumed.add(basename)
            # Recurse into row's child dir — sub-attachments
            _walk(
                zf,
                base + basename + "/",
                parent_uuid=matched.uuid,
                ancestor_page_uuid=matched.uuid,
                export=export,
                pos=[0],
            )
    # Attachments at row-dir level with no associated row → attach to db itself
    for f in sorted(o for o in files if not o.endswith((".md", ".csv"))):
        _add_attachment(zf, f, parent_uuid=db.uuid, export=export)


def _row_title(row: Row, db: Database) -> str:
    if not db.columns:
        return ""
    first = db.columns[0].name
    return str(row.values.get(first, "")).strip()


# ── markdown → blocks ──────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_TODO_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_NUM_RE = re.compile(r"^\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_IMG_RE = re.compile(r"^!\[(.*?)\]\((.+?)\)\s*$")
_DIVIDER_RE = re.compile(r"^-{3,}\s*$")
_CODE_FENCE_RE = re.compile(r"^```(\w*)\s*$")


def _parse_markdown(text: str, page_uuid: str, parent_uuid: str) -> list[Block]:
    """Line-based scanner. Not a full markdown spec — covers Notion's common shapes.

    Anything unrecognized becomes a `paragraph` block, so content is never silently
    dropped. The position path used for uuidv5 derivation is the block's index in
    this page; editing a paragraph rotates its uuid (intended).
    """
    blocks: list[Block] = []
    lines = text.splitlines()
    i = 0
    idx = 0

    # Notion's first non-blank line is usually the page title as `# Title`.
    # Skip the first heading if it sits at the top, since the page itself carries
    # the title already.
    skipped_title = False
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if not skipped_title:
            m = _HEADING_RE.match(line)
            if m and m.group(1) == "#":
                skipped_title = True
                i += 1
                continue
        # Code fence
        m = _CODE_FENCE_RE.match(line)
        if m:
            lang = m.group(1) or ""
            i += 1
            buf: list[str] = []
            while i < len(lines) and not _CODE_FENCE_RE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1  # consume closing fence
            content = "\n".join(buf)
            blocks.append(
                _mkblock(
                    page_uuid,
                    parent_uuid,
                    idx,
                    "code",
                    content,
                    extra={"language": lang},
                )
            )
            idx += 1
            continue
        # Image
        m = _IMG_RE.match(line)
        if m:
            alt, src = m.group(1), m.group(2)
            blocks.append(
                _mkblock(
                    page_uuid,
                    parent_uuid,
                    idx,
                    "image",
                    alt,
                    extra={"src": src, "alt": alt},
                )
            )
            idx += 1
            i += 1
            continue
        # Divider
        if _DIVIDER_RE.match(line):
            blocks.append(_mkblock(page_uuid, parent_uuid, idx, "divider", ""))
            idx += 1
            i += 1
            continue
        # Heading
        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            content = m.group(2).strip()
            blocks.append(
                _mkblock(
                    page_uuid,
                    parent_uuid,
                    idx,
                    f"heading_{level}",
                    content,
                    title=content,
                )
            )
            idx += 1
            i += 1
            continue
        # Todo (must check before bullet, since the prefix is `- [`)
        m = _TODO_RE.match(line)
        if m:
            checked = m.group(1).lower() == "x"
            content = m.group(2).strip()
            blocks.append(
                _mkblock(
                    page_uuid,
                    parent_uuid,
                    idx,
                    "todo",
                    content,
                    extra={"checked": checked},
                )
            )
            idx += 1
            i += 1
            continue
        # Bullet
        m = _BULLET_RE.match(line)
        if m:
            content = m.group(1).strip()
            blocks.append(_mkblock(page_uuid, parent_uuid, idx, "bulleted", content))
            idx += 1
            i += 1
            continue
        # Numbered
        m = _NUM_RE.match(line)
        if m:
            content = m.group(1).strip()
            blocks.append(_mkblock(page_uuid, parent_uuid, idx, "numbered", content))
            idx += 1
            i += 1
            continue
        # Quote
        m = _QUOTE_RE.match(line)
        if m:
            content = m.group(1).strip()
            blocks.append(_mkblock(page_uuid, parent_uuid, idx, "quote", content))
            idx += 1
            i += 1
            continue
        # Paragraph — accumulate lines until blank
        buf = [line]
        i += 1
        while i < len(lines) and lines[i].strip():
            # but stop if next line looks like a structural element
            nxt = lines[i]
            if (
                _HEADING_RE.match(nxt)
                or _TODO_RE.match(nxt)
                or _BULLET_RE.match(nxt)
                or _NUM_RE.match(nxt)
                or _QUOTE_RE.match(nxt)
                or _IMG_RE.match(nxt)
                or _CODE_FENCE_RE.match(nxt)
                or _DIVIDER_RE.match(nxt)
            ):
                break
            buf.append(nxt)
            i += 1
        content = "\n".join(buf).strip()
        if content:
            blocks.append(_mkblock(page_uuid, parent_uuid, idx, "paragraph", content))
            idx += 1
    return blocks


def _mkblock(
    page_uuid: str,
    parent_uuid: str,
    pos: int,
    kind: str,
    text: str,
    title: str | None = None,
    extra: dict | None = None,
) -> Block:
    block_uuid = derive_block(page_uuid, str(pos), text)
    return Block(
        uuid=block_uuid,
        parent_uuid=parent_uuid,
        page_uuid=page_uuid,
        kind=kind,
        pos=pos,
        text=text,
        title=title,
        extra=extra or {},
    )


# ── CSV → typed columns + rows ─────────────────────────────────────────────


def _dedupe_csvs(csv_paths: list[str]) -> list[str]:
    """When `Foo abc.csv` and `Foo abc_all.csv` coexist, keep only `_all`."""
    by_base: dict[str, str] = {}
    for p in csv_paths:
        stem = PurePosixPath(p).stem
        base = stem.removesuffix("_all")
        prev = by_base.get(base)
        if prev is None:
            by_base[base] = p
        else:
            # Prefer the _all one
            if PurePosixPath(p).stem.endswith("_all"):
                by_base[base] = p
    return list(by_base.values())


def _parse_csv(data: bytes, db_uuid: str) -> tuple[list[Column], list[Row]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], []
    raw_rows = list(reader)

    # Per-column value samples for type inference
    columns: list[Column] = []
    for col_idx, name in enumerate(header):
        values = [r[col_idx] if col_idx < len(r) else "" for r in raw_rows]
        sqlite_t, notion_t = _infer_types(values)
        columns.append(Column(name=name, sqlite_type=sqlite_t, notion_type=notion_t))

    rows: list[Row] = []
    for row_idx, raw in enumerate(raw_rows):
        values: dict[str, object] = {}
        for col_idx, col in enumerate(columns):
            raw_v = raw[col_idx] if col_idx < len(raw) else ""
            values[col.name] = _coerce(raw_v, col.sqlite_type, col.notion_type)
        row_uuid = derive_row(db_uuid, str(row_idx))
        rows.append(Row(uuid=row_uuid, values=values))
    return columns, rows


_INT_RE = re.compile(r"^-?\d+$")
_REAL_RE = re.compile(r"^-?\d+\.\d+$")
_BOOL_TRUE = {"yes", "true", "checked", "✓"}
_BOOL_FALSE = {"no", "false", "unchecked", ""}


def _infer_types(values: list[str]) -> tuple[str, str]:
    non_empty = [v for v in values if v.strip()]
    if not non_empty:
        return "TEXT", "text"
    if all(_INT_RE.match(v) for v in non_empty):
        return "INTEGER", "number"
    if all(_INT_RE.match(v) or _REAL_RE.match(v) for v in non_empty):
        return "REAL", "number"
    lower = [v.strip().lower() for v in values]
    if all(v in _BOOL_TRUE or v in _BOOL_FALSE for v in lower):
        return "INTEGER", "checkbox"
    # Multi-select-ish: comma-separated short tokens
    if all("," in v for v in non_empty) and all(
        len(v) < 200 for v in non_empty
    ):
        return "TEXT", "multi_select"
    return "TEXT", "text"


def _coerce(raw: str, sqlite_t: str, notion_t: str):
    s = raw.strip()
    if s == "" and sqlite_t != "TEXT":
        return None
    if sqlite_t == "INTEGER":
        if notion_t == "checkbox":
            return 1 if s.lower() in _BOOL_TRUE else 0
        return int(s)
    if sqlite_t == "REAL":
        return float(s)
    return raw  # TEXT: preserve original including any whitespace inside


# ── attachments ────────────────────────────────────────────────────────────


def _add_attachment(zf: zipfile.ZipFile, path: str, parent_uuid: str | None, export: Export) -> None:
    data = zf.read(path)
    sha = hashlib.sha256(data).hexdigest()
    uuid_str = derive_attachment(sha)
    mime, _ = mimetypes.guess_type(path)
    ext = PurePosixPath(path).suffix
    rel = f"{sha[:2]}/{sha}{ext}"
    export.attachments.append(
        Attachment(
            uuid=uuid_str,
            original_path=path,
            rel_path=rel,
            mime=mime,
            sha256=sha,
            size_bytes=len(data),
            data=data,
            parent_uuid=parent_uuid,
        )
    )


# ── name helpers ───────────────────────────────────────────────────────────


def _strip_hex_suffix(name: str) -> str:
    return _HEX_SUFFIX.sub("", name).strip()


_SAFE_TABLE_RE = re.compile(r"[^a-zA-Z0-9_]")


def _sanitize_table_name(name: str) -> str:
    safe = _SAFE_TABLE_RE.sub("_", name.lower()).strip("_")
    safe = re.sub(r"_+", "_", safe)
    if not safe:
        safe = "untitled"
    if safe[0].isdigit():
        safe = "t_" + safe
    return safe[:48]
