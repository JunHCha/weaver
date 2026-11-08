"""Validate the public eval set and print its counts: python -m weaver.evals."""

import json
import sys
from pathlib import Path

from .dataset import CASES_DIR, load_dataset, summarize


def main() -> None:
    directory = Path(sys.argv[1]) if len(sys.argv) > 1 else CASES_DIR
    print(json.dumps(summarize(load_dataset(directory)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
