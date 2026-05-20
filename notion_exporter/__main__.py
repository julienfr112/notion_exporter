# SPDX-License-Identifier: Apache-2.0
"""Enable `python -m notion_exporter ...`."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
