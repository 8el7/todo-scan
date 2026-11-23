"""Allow ``python -m todo_scan``."""

from todo_scan.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
