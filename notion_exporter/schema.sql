-- SPDX-License-Identifier: Apache-2.0
-- Output schema for notion-exporter. See README.md for column docs.

CREATE TABLE IF NOT EXISTS kv (
    uuid       TEXT PRIMARY KEY,
    parent     TEXT,
    kind       TEXT NOT NULL,
    pos        INTEGER NOT NULL,
    title      TEXT,
    text       TEXT,
    page_uuid  TEXT,
    json       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS kv_parent ON kv(parent);
CREATE INDEX IF NOT EXISTS kv_kind_page ON kv(kind, page_uuid);

CREATE TABLE IF NOT EXISTS notion_database (
    uuid         TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    table_name   TEXT NOT NULL UNIQUE,
    schema_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachment (
    uuid           TEXT PRIMARY KEY,
    original_path  TEXT NOT NULL,
    rel_path       TEXT NOT NULL,
    mime           TEXT,
    sha256         TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    blob           BLOB
);
CREATE INDEX IF NOT EXISTS attachment_sha ON attachment(sha256);

CREATE TABLE IF NOT EXISTS export_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
