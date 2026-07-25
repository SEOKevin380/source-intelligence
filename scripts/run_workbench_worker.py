#!/usr/bin/env python3
"""Run the durable newswire provider worker."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import NEWSWIRE_WORKBENCH_PATH
from newswire_workbench.run_worker import RunQueueWorker


def main() -> None:
    master = ROOT / "MBK_Project_Instructions_All_Platforms.txt"
    instructions = (
        master.read_text(encoding="utf-8") if master.exists() else ""
    )
    RunQueueWorker(
        NEWSWIRE_WORKBENCH_PATH,
        instructions,
    ).run_forever()


if __name__ == "__main__":
    main()
