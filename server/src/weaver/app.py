"""Resident process entry point; runtime implementation follows in later issues."""

import argparse
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI arguments without starting any services on import or help."""
    parser = argparse.ArgumentParser(
        prog="python -m weaver.app",
        description="Weaver server (Spinner and Loom). Runtime is not implemented yet.",
    )
    parser.parse_args(argv)
    parser.error("server runtime is not implemented yet")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
