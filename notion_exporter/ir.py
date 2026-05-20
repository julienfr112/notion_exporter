# SPDX-License-Identifier: Apache-2.0
"""Intermediate representation between parser and writer.

Parsers populate these dataclasses; the writer reads them and emits SQLite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Block:
    uuid: str
    parent_uuid: str
    page_uuid: str
    kind: str
    pos: int
    text: str = ""
    title: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Page:
    uuid: str
    title: str
    parent_uuid: str | None
    pos: int
    blocks: list[Block] = field(default_factory=list)
    children: list["Page | Database"] = field(default_factory=list)


@dataclass
class Column:
    name: str
    sqlite_type: str  # "TEXT" | "INTEGER" | "REAL"
    notion_type: str  # "text" | "select" | "multi_select" | "relation" | "date" | "checkbox" | "number" | ...


@dataclass
class Row:
    uuid: str
    values: dict[str, Any]
    blocks: list[Block] = field(default_factory=list)


@dataclass
class Database:
    uuid: str
    name: str
    parent_uuid: str | None
    pos: int
    table_name: str
    columns: list[Column] = field(default_factory=list)
    rows: list[Row] = field(default_factory=list)


@dataclass
class Attachment:
    uuid: str
    original_path: str
    rel_path: str
    mime: str | None
    sha256: str
    size_bytes: int
    data: bytes
    parent_uuid: str | None = None  # the page/row that referenced it, when known


@dataclass
class Export:
    pages: list[Page] = field(default_factory=list)
    databases: list[Database] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)
