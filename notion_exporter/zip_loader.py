# SPDX-License-Identifier: Apache-2.0
"""Open a Notion export zip and pick the right parser version.

Notion has changed its export format repeatedly (notable shifts in 2022, 2023).
The version-detect step here exists so a silent format change becomes a loud
ValueError, not silent data corruption downstream.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass


@dataclass
class LoadedExport:
    zf: zipfile.ZipFile
    parser_version: str
    source_path: str


def open_export(path: str) -> LoadedExport:
    zf = zipfile.ZipFile(path, "r")
    version = _detect_format(zf)
    return LoadedExport(zf=zf, parser_version=version, source_path=path)


def _detect_format(zf: zipfile.ZipFile) -> str:
    """Heuristic: 2024-format Markdown & CSV has top-level .md/.csv files with
    a 32-hex token in the filename. HTML exports have .html files. Older Notion
    "single workspace folder" wraps everything in one parent directory.

    For v1 we only support the 2024 Markdown & CSV format.
    """
    names = zf.namelist()
    if not names:
        raise ValueError("empty zip")

    if any(n.endswith(".html") for n in names):
        raise ValueError(
            "HTML-formatted Notion export detected. notion-exporter only supports "
            "the 'Markdown & CSV' export format. Re-export from Notion choosing "
            "'Markdown & CSV' as the format."
        )

    has_md = any(n.endswith(".md") for n in names)
    if not has_md:
        raise ValueError("no .md files in zip — is this really a Notion export?")

    return "v2024_md_csv"


def load_parser(version: str):
    if version == "v2024_md_csv":
        from .parsers import v2024_md_csv

        return v2024_md_csv
    raise ValueError(f"no parser for format version {version!r}")
