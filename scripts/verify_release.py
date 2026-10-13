"""Verify the project ZIP in a separate directory and fresh Python environment.

No API keys or external Loghub root are passed to subprocesses. Dependencies
are installed from requirements.txt; paid model success is NOT measured here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=ROOT / "release" / "LogRCA-Spring-Tool.zip")
    args = parser.parse_args()
    environment = dict(os.environ)
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LOGHUB_ROOT", "PYTHONPATH", "PYTHONHOME"):
        environment.pop(name, None)
    results = {"archive": str(args.archive.resolve()), "paid_api_verified": False, "steps": []}
    with tempfile.TemporaryDirectory(prefix="logrca-release-check-") as temporary:
        workspace = Path(temporary).resolve() / "project"
        workspace.mkdir()
        with zipfile.ZipFile(args.archive) as archive:
            if any(not (workspace / name).resolve().is_relative_to(workspace) for name in archive.namelist()):
                raise ValueError("archive contains paths outside the target workspace")
            archive.extractall(workspace)
        venv = Path(temporary) / "venv"
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        steps = [
            ("create_fresh_venv", [sys.executable, "-m", "venv", str(venv)]),
            ("install_dependencies", [str(python), "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", "requirements.txt"]),
            ("standalone_tests", [str(python), "-X", "utf8", "-m", "pytest", "--tb=short"]),
            ("offline_json_file", [str(python), "-X", "utf8", "-m", "logrca", "analyze", "--path", "data/spring_db_pool.jsonl", "--output-dir", "outputs/check", "--json"]),
        ]
        for name, command in steps:
            print(f"验证步骤：{name}", flush=True)
            log = Path(temporary) / f"{name}.log"
            with log.open("w", encoding="utf-8") as handle:
                result = subprocess.run(command, cwd=workspace, env=environment, stdout=handle,
                    stderr=subprocess.STDOUT, timeout=600 if name == "install_dependencies" else 180)
            output = log.read_text(encoding="utf-8", errors="replace")
            entry = {"name": name, "exit_code": result.returncode}
            if name == "standalone_tests":
                match = re.search(r"\d+ passed[^\n]*", output)
                entry["summary"] = match.group(0) if match else "see test output"
                print(entry["summary"], flush=True)
            if name == "offline_json_file" and result.returncode == 0:
                payload = json.loads(output)
                entry["root_cause_code"] = payload["reports"][0]["root_cause_code"]
                if entry["root_cause_code"] != "db_pool_exhausted":
                    raise RuntimeError("standalone file diagnosis did not produce expected offline root")
            results["steps"].append(entry)
            if result.returncode:
                # Report useful errors while bounding/redacting installer URLs.
                tail = output[-3000:]
                tail = re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", tail)
                print(tail)
                raise RuntimeError(f"release verification failed: {name}")
    target = ROOT / "release" / "reproducibility_check.json"
    target.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"独立环境验证完成：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
