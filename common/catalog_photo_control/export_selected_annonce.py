from __future__ import annotations

import sys

from .final_catalog_ops import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
