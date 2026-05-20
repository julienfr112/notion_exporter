# SPDX-License-Identifier: Apache-2.0
"""Parser for Notion's 2024 'Markdown & CSV' export format.

Structure: every page is a `<Title> <32hex>.md` file at some path. If the page
has children (sub-pages, sub-databases, attachments), they live in a sibling
directory `<Title> <32hex>/`. Databases are `<Title> <32hex>.csv` files (Notion
emits a `_all` variant alongside the per-view file; we prefer `_all`). Per-row
page content lives in `<DBTitle> <32hex>/<RowTitle> <32hex>.md`.

Two-phase design:
  Phase 1 — walk the zip, classify every entry, collect attachments, build a
            (zip path → uuid) index for pages and database rows. Queue markdown
            bodies for phase 2 but do NOT parse them yet.
  Phase 2 — with the full attachment + page index built, parse each markdown
            body. Inline image/file/page references can now be resolved to the
            corresponding attachment or page uuid and stored on block.extra.

The two-phase split is what enables `![](pic.png)` and `[Other Page](Other Page abc.md)`
to carry a stable cross-reference, not just an opaque relative path.
"""
from __future__ import annotations

import csv
import hashlib
import io
import mimetypes
import os
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlparse

from ..ir import Attachment, Block, Column, Database, Export, Page, Row
from ..uuid_strategy import (
    derive_attachment,
    derive_block,
    derive_row,
    extract_from_filename,
)

PARSER_VERSION = "v2024_md_csv"

_HEX_SUFFIX = re.compile(r"\s+[0-9a-fA-F]{32}$")


# ── public entry ───────────────────────────────────────────────────────────


def parse(loaded, export: Export) -> None:
    zf = loaded.zf
    root = _detect_root_prefix(zf.namelist())

    pending: list[tuple[Any, str, str]] = []  # (owner, md_path, parent_uuid_for_blocks)
    md_path_to_uuid: dict[str, str] = {}
    table_names_used: set[str] = set()

    _walk(
        zf,
        root,
        parent_uuid=None,
        ancestor_page_uuid=None,
        export=export,
        pos=[0],
        pending=pending,
        md_path_to_uuid=md_path_to_uuid,
        table_names_used=table_names_used,
    )

    att_by_path = {a.original_path: a for a in export.attachments}
    for owner, md_path, parent_uuid in pending:
        text = zf.read(md_path).decode("utf-8-sig", errors="replace")
        # Notion resolves relative links inside a page against the page's
        # *companion directory* (the same-named subdir holding its children),
        # NOT the directory containing the .md file. So `Foo bar.md` references
        # are resolved against `Foo bar/`.
        md_dir = str(PurePosixPath(md_path).with_suffix(""))
        owner.blocks = _parse_markdown(
            text,
            page_uuid=owner.uuid,
            parent_uuid=parent_uuid,
            md_dir=md_dir,
            att_by_path=att_by_path,
            md_path_to_uuid=md_path_to_uuid,
        )


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
    *,
    parent_uuid: str | None,
    ancestor_page_uuid: str | None,
    export: Export,
    pos: list[int],
    pending: list,
    md_path_to_uuid: dict[str, str],
    table_names_used: set[str],
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
            continue
        title = _strip_hex_suffix(basename)
        p = pos[0]
        pos[0] += 1
        page = Page(uuid=page_uuid, title=title, parent_uuid=parent_uuid, pos=p)
        export.pages.append(page)
        md_path_to_uuid[md] = page_uuid
        pending.append((page, md, page_uuid))
        if basename in dirs:
            consumed.add(basename)
            _walk(
                zf,
                base + basename + "/",
                parent_uuid=page_uuid,
                ancestor_page_uuid=page_uuid,
                export=export,
                pos=[0],
                pending=pending,
                md_path_to_uuid=md_path_to_uuid,
                table_names_used=table_names_used,
            )

    for csv_path in sorted(csv_files):
        basename = PurePosixPath(csv_path).stem
        dir_basename = basename.removesuffix("_all")
        db_uuid = extract_from_filename(dir_basename)
        if db_uuid is None:
            continue
        name = _strip_hex_suffix(dir_basename)
        table_name = _make_unique_table_name(name, db_uuid, table_names_used)
        p = pos[0]
        pos[0] += 1
        db = Database(
            uuid=db_uuid,
            name=name,
            parent_uuid=parent_uuid,
            pos=p,
            table_name=table_name,
        )
        # Pre-walk row dir so per-row .md filenames give us authoritative UUIDs
        row_dir = base + dir_basename + "/" if dir_basename in dirs else None
        title_to_row_md: dict[str, tuple[str, str]] = {}
        if row_dir is not None:
            consumed.add(dir_basename)
            title_to_row_md, row_attachments = _scan_row_dir(zf, row_dir)
            for a_path in row_attachments:
                _add_attachment(zf, a_path, parent_uuid=db_uuid, export=export)
        cols, rows = _parse_csv(
            zf.read(csv_path),
            db_uuid=db_uuid,
            title_to_row_md=title_to_row_md,
        )
        db.columns = cols
        db.rows = rows
        for row in rows:
            md_path = row.values.get("__row_md_path__")
            if md_path:
                # The CSV row will keep its actual data only — drop the sentinel
                del row.values["__row_md_path__"]
                md_path_to_uuid[md_path] = row.uuid
                pending.append((row, md_path, row.uuid))
                # Recurse into sub-row dir for attachments / nested pages
                row_basename = PurePosixPath(md_path).stem
                row_subdir = base + dir_basename + "/" + row_basename
                if (
                    row_dir is not None
                    and row_basename in _list_dir(zf, row_dir)[1]
                ):
                    _walk(
                        zf,
                        row_subdir + "/",
                        parent_uuid=row.uuid,
                        ancestor_page_uuid=row.uuid,
                        export=export,
                        pos=[0],
                        pending=pending,
                        md_path_to_uuid=md_path_to_uuid,
                        table_names_used=table_names_used,
                    )
        export.databases.append(db)

    for f in sorted(other_files):
        _add_attachment(zf, f, parent_uuid=ancestor_page_uuid, export=export)

    # Orphan dirs — recurse so nothing is lost
    for d in sorted(dirs - consumed):
        _walk(
            zf,
            base + d + "/",
            parent_uuid=parent_uuid,
            ancestor_page_uuid=ancestor_page_uuid,
            export=export,
            pos=[0],
            pending=pending,
            md_path_to_uuid=md_path_to_uuid,
            table_names_used=table_names_used,
        )


def _scan_row_dir(
    zf: zipfile.ZipFile, row_dir: str
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    """Return (title → (hex_uuid, md_path), [attachment_paths])."""
    files, _ = _list_dir(zf, row_dir)
    by_title: dict[str, tuple[str, str]] = {}
    attachments: list[str] = []
    for f in files:
        if f.endswith(".md"):
            basename = PurePosixPath(f).stem
            hex_uuid = extract_from_filename(basename)
            if hex_uuid is None:
                continue
            title = _strip_hex_suffix(basename)
            by_title[title] = (hex_uuid, f)
        elif not f.endswith(".csv"):
            attachments.append(f)
    return by_title, attachments


# ── markdown → blocks ──────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
_TODO_RE = re.compile(r"^-\s+\[([ xX])\]\s+(.*)$")
_BULLET_RE = re.compile(r"^[-*+]\s+(.*)$")
_NUM_RE = re.compile(r"^\d+\.\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_IMG_RE = re.compile(r"^!\[(.*?)\]\((.+?)\)\s*$")
_LINK_LINE_RE = re.compile(r"^\[(.+?)\]\((.+?)\)\s*$")
_LINK_INLINE_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_DIVIDER_RE = re.compile(r"^-{3,}\s*$")
_CODE_FENCE_RE = re.compile(r"^```(\w*)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?(\s*\|\s*:?-{2,}:?)+\s*\|?\s*$")
_HTML_OPEN_RE = re.compile(r"^\s*<(details|aside)(\s[^>]*)?>\s*$")
_HTML_CLOSE_RE = re.compile(r"^\s*</(details|aside)>\s*$")
_SUMMARY_RE = re.compile(r"^\s*<summary>(.*?)</summary>\s*$")


def _parse_markdown(
    text: str,
    *,
    page_uuid: str,
    parent_uuid: str,
    md_dir: str,
    att_by_path: dict[str, Attachment],
    md_path_to_uuid: dict[str, str],
) -> list[Block]:
    """Line-based scanner that recognizes Notion's common block kinds.

    Unrecognized lines fall through to `paragraph` so content is never silently
    dropped. Image/file/page references are resolved against the export index so
    blocks carry `attachment_uuid` / `target_page_uuid` in their extras.
    """
    blocks: list[Block] = []
    lines = text.splitlines()
    state = _ParseState(
        lines=lines,
        i=0,
        idx=0,
        page_uuid=page_uuid,
        parent_uuid=parent_uuid,
        md_dir=md_dir,
        att_by_path=att_by_path,
        md_path_to_uuid=md_path_to_uuid,
    )
    _skip_leading_title(state)
    while state.i < len(lines):
        before = state.i
        produced = _try_one_block(state)
        if produced:
            blocks.extend(produced)
            # Only the first produced block consumes an outer-level position;
            # children of a toggle/callout are sibling rows in the flat list
            # but carry their own (parent_uuid, pos) and don't shift the
            # outer position counter.
            state.idx += 1
        if state.i == before:
            state.i += 1
    return blocks


class _ParseState:
    __slots__ = (
        "lines", "i", "idx",
        "page_uuid", "parent_uuid", "md_dir",
        "att_by_path", "md_path_to_uuid",
    )

    def __init__(self, *, lines, i, idx, page_uuid, parent_uuid, md_dir,
                 att_by_path, md_path_to_uuid):
        self.lines = lines
        self.i = i
        self.idx = idx
        self.page_uuid = page_uuid
        self.parent_uuid = parent_uuid
        self.md_dir = md_dir
        self.att_by_path = att_by_path
        self.md_path_to_uuid = md_path_to_uuid


def _skip_leading_title(state: _ParseState) -> None:
    """Notion's first non-blank line is usually `# Title` — the page already
    carries that, so we suppress it from the block list."""
    while state.i < len(state.lines) and not state.lines[state.i].strip():
        state.i += 1
    if state.i < len(state.lines):
        m = _HEADING_RE.match(state.lines[state.i])
        if m and m.group(1) == "#":
            state.i += 1


def _try_one_block(state: _ParseState) -> list[Block]:
    """Consume lines for one block. Returns a list of blocks (1 in the common
    case; multiple for HTML containers like toggles/callouts that produce a
    parent + nested children). Empty list means we just advanced past blank
    lines without producing anything."""
    lines = state.lines
    i = state.i

    if not lines[i].strip():
        state.i += 1
        return []

    line = lines[i]

    m = _HTML_OPEN_RE.match(line)
    if m:
        return _consume_html_container(state, m.group(1))

    m = _CODE_FENCE_RE.match(line)
    if m:
        return [_consume_code(state, m.group(1) or "")]

    m = _IMG_RE.match(line)
    if m:
        state.i += 1
        return [_make_image_block(state, m.group(1), m.group(2))]

    m = _LINK_LINE_RE.match(line)
    if m:
        state.i += 1
        return [_make_link_block(state, m.group(1), m.group(2))]

    if _DIVIDER_RE.match(line):
        state.i += 1
        return [_mkblock(state, "divider", "")]

    m = _HEADING_RE.match(line)
    if m:
        state.i += 1
        level = len(m.group(1))
        content = m.group(2).strip()
        return [_mkblock(state, f"heading_{level}", content, title=content)]

    m = _TODO_RE.match(line)
    if m:
        state.i += 1
        return [_mkblock(
            state, "todo", m.group(2).strip(),
            extra={"checked": m.group(1).lower() == "x"},
        )]

    m = _BULLET_RE.match(line)
    if m:
        state.i += 1
        return [_mkblock(state, "bulleted", m.group(1).strip())]

    m = _NUM_RE.match(line)
    if m:
        state.i += 1
        return [_mkblock(state, "numbered", m.group(1).strip())]

    m = _QUOTE_RE.match(line)
    if m:
        state.i += 1
        return [_mkblock(state, "quote", m.group(1).strip())]

    if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
        return [_consume_table(state)]

    return [_consume_paragraph(state)]


def _consume_html_container(state: _ParseState, tag: str) -> list[Block]:
    """Handles `<details>...</details>` (toggle) and `<aside>...</aside>` (callout).

    Returns [container, ...children]. Children carry `parent=container.uuid`,
    so when written into kv they form a sub-tree under the container row.
    """
    kind = "toggle" if tag == "details" else "callout"
    state.i += 1  # consume open tag
    summary_text = ""
    body_lines: list[str] = []
    depth = 1
    while state.i < len(state.lines):
        line = state.lines[state.i]
        if _HTML_OPEN_RE.match(line):
            depth += 1
            body_lines.append(line)
            state.i += 1
            continue
        if _HTML_CLOSE_RE.match(line):
            depth -= 1
            if depth == 0:
                state.i += 1
                break
            body_lines.append(line)
            state.i += 1
            continue
        m = _SUMMARY_RE.match(line)
        if m and not summary_text:
            summary_text = m.group(1).strip()
            state.i += 1
            continue
        body_lines.append(line)
        state.i += 1

    container = _mkblock(state, kind, summary_text, title=summary_text or None)
    if not body_lines:
        return [container]
    children = _parse_markdown(
        "\n".join(body_lines),
        page_uuid=state.page_uuid,
        parent_uuid=container.uuid,
        md_dir=state.md_dir,
        att_by_path=state.att_by_path,
        md_path_to_uuid=state.md_path_to_uuid,
    )
    return [container, *children]


def _consume_code(state: _ParseState, lang: str) -> Block:
    state.i += 1  # opening fence
    buf: list[str] = []
    while state.i < len(state.lines) and not _CODE_FENCE_RE.match(state.lines[state.i]):
        buf.append(state.lines[state.i])
        state.i += 1
    if state.i < len(state.lines):
        state.i += 1  # closing fence
    return _mkblock(state, "code", "\n".join(buf), extra={"language": lang})


def _consume_table(state: _ParseState) -> Block:
    headers = _split_table_row(state.lines[state.i])
    state.i += 2  # header + separator
    rows: list[list[str]] = []
    while state.i < len(state.lines):
        line = state.lines[state.i]
        if not line.strip() or "|" not in line:
            break
        rows.append(_split_table_row(line))
        state.i += 1
    flat = " | ".join(" ".join(c for c in row) for row in [headers, *rows])
    return _mkblock(
        state, "table", flat, extra={"headers": headers, "rows": rows},
    )


def _split_table_row(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _consume_paragraph(state: _ParseState) -> Block:
    lines = state.lines
    buf = [lines[state.i]]
    state.i += 1
    while state.i < len(lines) and lines[state.i].strip():
        nxt = lines[state.i]
        if (
            _HEADING_RE.match(nxt)
            or _TODO_RE.match(nxt)
            or _BULLET_RE.match(nxt)
            or _NUM_RE.match(nxt)
            or _QUOTE_RE.match(nxt)
            or _IMG_RE.match(nxt)
            or _LINK_LINE_RE.match(nxt)
            or _CODE_FENCE_RE.match(nxt)
            or _DIVIDER_RE.match(nxt)
            or _HTML_OPEN_RE.match(nxt)
            or _HTML_CLOSE_RE.match(nxt)
        ):
            break
        buf.append(nxt)
        state.i += 1
    content = "\n".join(buf).strip()
    extra: dict[str, Any] = {}
    inline = _collect_inline_links(content, state)
    if inline:
        extra["links"] = inline
    return _mkblock(state, "paragraph", content, extra=extra)


def _collect_inline_links(text: str, state: _ParseState) -> list[dict]:
    """Find markdown links inside paragraph text, resolve where possible."""
    out: list[dict] = []
    for m in _LINK_INLINE_RE.finditer(text):
        if m.start() > 0 and text[m.start() - 1] == "!":
            # this is an image — skip; image-only-paragraphs are caught earlier
            continue
        label, href = m.group(1), m.group(2)
        entry: dict[str, Any] = {"label": label, "href": href}
        ref = _resolve_local(href, state)
        if ref:
            entry.update(ref)
        out.append(entry)
    return out


# ── link resolution ────────────────────────────────────────────────────────


def _resolve_local(href: str, state: _ParseState) -> dict | None:
    """If href is a local relative path, look it up in the export index.
    Returns {target_page_uuid, ...} or {attachment_uuid, ...} or None for
    external URLs / unresolved references.
    """
    parsed = urlparse(href)
    if parsed.scheme in ("http", "https", "mailto", "ftp"):
        return None
    decoded = unquote(href)
    if not decoded or decoded.startswith("#"):
        return None
    # Absolute path inside zip (rare in Notion exports, but handle it)
    if decoded.startswith("/"):
        candidate = decoded.lstrip("/")
    else:
        joined = (PurePosixPath(state.md_dir) / decoded).as_posix() if state.md_dir else decoded
        candidate = os.path.normpath(joined).replace("\\", "/")
    if candidate in state.md_path_to_uuid:
        return {"target_page_uuid": state.md_path_to_uuid[candidate]}
    att = state.att_by_path.get(candidate)
    if att is not None:
        return {"attachment_uuid": att.uuid}
    return None


def _make_image_block(state: _ParseState, alt: str, src: str) -> Block:
    extra: dict[str, Any] = {"src": src, "alt": alt}
    ref = _resolve_local(src, state)
    if ref:
        extra.update(ref)
    return _mkblock(state, "image", alt, extra=extra)


def _make_link_block(state: _ParseState, label: str, href: str) -> Block:
    """Standalone link on its own line. Classify into page_link, file, or bookmark."""
    parsed = urlparse(href)
    extra: dict[str, Any] = {"href": href, "label": label}
    ref = _resolve_local(href, state)
    if ref and "target_page_uuid" in ref:
        extra.update(ref)
        return _mkblock(state, "page_link", label, title=label, extra=extra)
    if ref and "attachment_uuid" in ref:
        extra.update(ref)
        return _mkblock(state, "file", label, title=label, extra=extra)
    if parsed.scheme in ("http", "https"):
        return _mkblock(state, "bookmark", href, title=label or None, extra=extra)
    # unresolved local — keep as paragraph-ish with the raw link in text
    return _mkblock(state, "paragraph", f"[{label}]({href})", extra=extra)


# ── block construction ────────────────────────────────────────────────────


def _mkblock(
    state: _ParseState,
    kind: str,
    text: str,
    *,
    title: str | None = None,
    extra: dict | None = None,
) -> Block:
    # Include parent_uuid in the derivation so nested blocks can't collide
    # with top-level blocks at the same position.
    block_uuid = derive_block(
        state.page_uuid, f"{state.parent_uuid}/{state.idx}", text
    )
    return Block(
        uuid=block_uuid,
        parent_uuid=state.parent_uuid,
        page_uuid=state.page_uuid,
        kind=kind,
        pos=state.idx,
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
            if PurePosixPath(p).stem.endswith("_all"):
                by_base[base] = p
    return list(by_base.values())


def _parse_csv(
    data: bytes,
    db_uuid: str,
    title_to_row_md: dict[str, tuple[str, str]],
) -> tuple[list[Column], list[Row]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        return [], []
    header = _dedupe_column_names([h.strip() for h in header])
    raw_rows = list(reader)

    columns: list[Column] = []
    for col_idx, name in enumerate(header):
        values = [r[col_idx] if col_idx < len(r) else "" for r in raw_rows]
        sqlite_t, notion_t = _infer_types(values)
        columns.append(Column(name=name, sqlite_type=sqlite_t, notion_type=notion_t))

    title_col = columns[0].name if columns else None
    rows: list[Row] = []
    for row_idx, raw in enumerate(raw_rows):
        values: dict[str, object] = {}
        for col_idx, col in enumerate(columns):
            raw_v = raw[col_idx] if col_idx < len(raw) else ""
            values[col.name] = _coerce(raw_v, col.sqlite_type, col.notion_type)
        # Authoritative UUID from the per-row .md filename when available
        row_title = str(values.get(title_col, "")).strip() if title_col else ""
        md_match = title_to_row_md.get(row_title)
        if md_match is not None:
            hex_uuid, md_path = md_match
            row_uuid = hex_uuid
            values["__row_md_path__"] = md_path
        else:
            row_uuid = derive_row(db_uuid, str(row_idx))
        rows.append(Row(uuid=row_uuid, values=values))
    return columns, rows


def _dedupe_column_names(names: list[str]) -> list[str]:
    """Notion shouldn't produce dupes, but a hand-edited CSV might. Disambiguate."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for n in names:
        base = n or "col"
        if base not in seen:
            seen[base] = 1
            out.append(base)
        else:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
    return out


_INT_RE = re.compile(r"^-?\d+$")
_REAL_RE = re.compile(r"^-?\d+\.\d+$")
_BOOL_TRUE = {"yes", "true", "checked", "✓"}
_BOOL_FALSE = {"no", "false", "unchecked"}


def _infer_types(values: list[str]) -> tuple[str, str]:
    non_empty = [v for v in values if v.strip()]
    if not non_empty:
        return "TEXT", "text"
    if all(_INT_RE.match(v) for v in non_empty):
        return "INTEGER", "number"
    if all(_INT_RE.match(v) or _REAL_RE.match(v) for v in non_empty):
        return "REAL", "number"
    lower_non_empty = [v.strip().lower() for v in non_empty]
    if all(v in _BOOL_TRUE or v in _BOOL_FALSE for v in lower_non_empty):
        return "INTEGER", "checkbox"
    # Multi-select: ANY non-empty value contains a comma + values are short
    if any("," in v for v in non_empty) and all(len(v) < 200 for v in non_empty):
        return "TEXT", "multi_select"
    # Select: small fixed vocabulary (≤ 12 distinct values), no commas, short tokens
    distinct = set(non_empty)
    if (
        len(distinct) <= 12
        and len(distinct) < len(non_empty)
        and all(len(v) < 80 for v in distinct)
    ):
        return "TEXT", "select"
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
    return raw


# ── attachments ────────────────────────────────────────────────────────────


def _add_attachment(
    zf: zipfile.ZipFile, path: str, parent_uuid: str | None, export: Export
) -> None:
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


def _make_unique_table_name(name: str, db_uuid: str, used: set[str]) -> str:
    """Two Notion DBs can share a name. Disambiguate the second one with the
    first 8 hex chars of its uuid so each gets its own SQLite table."""
    candidate = "data_" + _sanitize_table_name(name)
    if candidate not in used:
        used.add(candidate)
        return candidate
    suffix = db_uuid.replace("-", "")[:8]
    candidate = f"data_{_sanitize_table_name(name)}_{suffix}"
    used.add(candidate)
    return candidate
