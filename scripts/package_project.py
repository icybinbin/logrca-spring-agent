"""Build an independent project ZIP from an explicit source-only allowlist."""
from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "README.md", "QUICKSTART.md", ".gitignore", "pytest.ini", "requirements.txt",
    "requirements-openai.txt", "streamlit_app.py", "run_web.py", "run_demo.py",
    "run_stage1.py", "agent_core.py", "log_tools.py",
    "examples/deepseek_agent_demo.py", "examples/deepseek_connection_check.py",
    "docs/ARCHITECTURE.md", "docs/EVALUATION.md", "docs/DATA_SOURCES.md",
    "docs/PUBLIC_SAMPLES.md", "data/public_hikari_1546_source.json",
}
PATTERNS = ("logrca/*.py", "tests/*.py", "scripts/*.py", "data/*.log", "data/*.jsonl", ".github/workflows/*.yml")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release" / "LogRCA-Spring-Tool.zip")
    args = parser.parse_args()
    selected = {ROOT / name for name in FILES}
    for pattern in PATTERNS:
        selected.update(ROOT.glob(pattern))
    selected = sorted(path for path in selected if path.is_file() and not path.is_symlink())
    target = args.output.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in selected:
            archive.write(path, path.relative_to(ROOT).as_posix())
    print(f"已打包 {len(selected)} 个项目文件：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
