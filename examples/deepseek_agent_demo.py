"""Run the Spring -> DeepSeek -> tools -> audit -> report path from any cwd.

Pass additional analyze flags, e.g. --mode single or --json. There is no fake
API response in this entry point. It reads DEEPSEEK_API_KEY in this process.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from logrca.cli import main


if __name__ == "__main__":
    raise SystemExit(main([
        "analyze", "--path", str(PROJECT_ROOT / "data" / "spring_db_pool.log"),
        "--format", "spring", "--backend", "deepseek", "--model", "deepseek-flash",
        "--mode", "multi", "--max-incidents", "1",
        "--output-dir", str(PROJECT_ROOT / "outputs" / "deepseek_spring"),
        *sys.argv[1:],
    ]))
