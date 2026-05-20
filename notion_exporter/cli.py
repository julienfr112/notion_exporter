# SPDX-License-Identifier: Apache-2.0
"""CLI entry point: `notion-exporter convert <input.zip> <output.sqlite> [--embed-attachments]`."""
from __future__ import annotations

import argparse
import sys

from . import __version__
from .ir import Export
from .writer import write
from .zip_loader import load_parser, open_export


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="notion-exporter",
        description="Convert a Notion .zip export to a portable SQLite database.",
    )
    parser.add_argument(
        "--version", action="version", version=f"notion-exporter {__version__}"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    convert = sub.add_parser("convert", help="convert a Notion .zip export to SQLite")
    convert.add_argument("input_zip")
    convert.add_argument("output_sqlite")
    convert.add_argument(
        "--embed-attachments",
        action="store_true",
        help="store attachment bytes as SQLite BLOBs instead of writing a sidecar directory",
    )
    convert.add_argument(
        "--attachments-dir",
        default=None,
        help="directory for sidecar attachments (default: <output_sqlite>.attachments)",
    )
    convert.add_argument(
        "--quiet", action="store_true", help="suppress progress output"
    )

    args = parser.parse_args(argv)
    if args.cmd == "convert":
        return _cmd_convert(args)
    return 2


def _cmd_convert(args) -> int:
    attachments_dir = args.attachments_dir
    if attachments_dir is None and not args.embed_attachments:
        attachments_dir = args.output_sqlite + ".attachments"

    log = (lambda *a, **k: None) if args.quiet else _make_logger()

    log(f"opening {args.input_zip}")
    loaded = open_export(args.input_zip)
    log(f"detected format {loaded.parser_version}")
    parser_mod = load_parser(loaded.parser_version)

    export = Export()
    log("parsing")
    parser_mod.parse(loaded, export)
    loaded.zf.close()

    log(
        f"  → {len(export.pages)} pages, {len(export.databases)} databases, "
        f"{len(export.attachments)} attachments"
    )

    log(f"writing {args.output_sqlite}")
    write(
        export,
        args.output_sqlite,
        attachments_dir=attachments_dir,
        embed_attachments=args.embed_attachments,
        parser_version=loaded.parser_version,
        source_zip_path=args.input_zip,
    )
    if not args.embed_attachments and export.attachments:
        log(f"  attachments → {attachments_dir}/")
    log("done")
    return 0


def _make_logger():
    try:
        from rich.console import Console

        console = Console(stderr=True)
        return lambda msg: console.print(msg)
    except ImportError:
        return lambda msg: print(msg, file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
