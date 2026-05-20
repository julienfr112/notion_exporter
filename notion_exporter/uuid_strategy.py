# SPDX-License-Identifier: Apache-2.0
"""UUID derivation. Pure functions — easy to test, easy to reason about.

Pages get Notion's own UUID (extracted from filenames like `Page Name abc…def.md`).
Sub-page blocks get a deterministic uuidv5 so re-runs of the exporter produce
identical UUIDs — that's what gives Dispatch (and any other consumer) the
ability to diff-import an updated export.
"""
from __future__ import annotations

import hashlib
import re
import uuid

# Stable namespace for derived UUIDs. DO NOT change — every derivation downstream
# (including any Dispatch instance that has already imported an export) depends
# on this constant staying fixed.
NAMESPACE_DISPATCH = uuid.UUID("6e2f8c4a-1b9d-4e7a-9b3d-4a5c6d7e8f90")

_HEX32 = re.compile(r"([0-9a-fA-F]{32})")


def normalize(notion_id: str) -> str:
    """Turn a 32-hex Notion id (with or without hyphens) into canonical 8-4-4-4-12 form."""
    hex_only = notion_id.replace("-", "").lower()
    if len(hex_only) != 32 or not all(c in "0123456789abcdef" for c in hex_only):
        raise ValueError(f"not a 32-hex Notion id: {notion_id!r}")
    return str(uuid.UUID(hex_only))


def extract_from_filename(name: str) -> str | None:
    """Notion encodes a page's UUID as the trailing 32-hex token in its filename.

    Example: `My Page abc123def456abc123def456abc12345.md` → that 32-hex run.
    Returns None if no such token is found.
    """
    match = _HEX32.search(name)
    if not match:
        return None
    return normalize(match.group(1))


def derive(*parts: str) -> str:
    """Deterministic uuidv5 from string parts joined by '/'. Used for sub-page blocks."""
    return str(uuid.uuid5(NAMESPACE_DISPATCH, "/".join(parts)))


def derive_block(page_uuid: str, path_in_tree: str, content: str) -> str:
    """UUID for a block within a page.

    `path_in_tree` is a stable index path like '0/2/1' (3rd child of 2nd child of root).
    `content` is the block's textual content; its hash makes the UUID rotate when
    the block is edited, which is the desired semantics for diff-import.
    """
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return derive(page_uuid, path_in_tree, content_hash)


def derive_attachment(sha256_hex: str) -> str:
    return derive("attachment", sha256_hex)


def derive_row(database_uuid: str, row_key: str) -> str:
    """Database row UUID. row_key is Notion's own row id when present, else the row index."""
    return derive("row", database_uuid, row_key)
