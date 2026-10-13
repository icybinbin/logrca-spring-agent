"""Start the local tool from any working directory using this Python."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parent
    if importlib.util.find_spec("streamlit") is None:
        print(f"请先安装依赖：python -m pip install -r \"{root / 'requirements.txt'}\"")
        return 2
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(root / "streamlit_app.py"),
        "--server.address", "127.0.0.1", "--server.port", "8501", "--server.headless", "true", *sys.argv[1:]], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
