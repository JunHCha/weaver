"""PyInstaller entry point: the frozen binary behaves like `python -m weaver.app`."""

import sys

from weaver.app import main

if __name__ == "__main__":
    sys.exit(main())
