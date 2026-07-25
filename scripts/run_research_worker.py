#!/usr/bin/env python3
"""Run the durable Source Intelligence research worker."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import DB_PATH
from research_worker import ResearchQueueWorker


def main() -> None:
    ResearchQueueWorker(DB_PATH).run_forever()


if __name__ == "__main__":
    main()
