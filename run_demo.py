"""Convenience entry point; equivalent to ``python -m logrca demo``."""

from logrca.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["demo"]))
