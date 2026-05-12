#!/usr/bin/env python3
"""R3-C-05: --db/--repo accept "~/foo" literally instead of expanding $HOME.

argparse `type=Path` produces Path('~/foo') — a relative path with a
literal '~' as the top-level component. When the report/reconcile code
later does `db_path.parent.mkdir(parents=True, exist_ok=True)`, it
creates a literal `~/` directory in CWD instead of writing to $HOME.

Fix: custom `type=` callable that calls `.expanduser()`.

Exit 0 = REPRODUCED (parsed path is a literal '~/...', CWD-relative).
Exit 1 = NOT REPRODUCED (parsed path is absolute, under $HOME).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _harness import report  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pm_agent import cli  # noqa: E402


def main() -> int:
    ap, _p_loop, _p_dash = cli._build_parser()
    args = ap.parse_args(["loop", "report", "--db", "~/some-fake-db.sqlite"])

    db = args.db
    # Buggy: Path('~/some-fake-db.sqlite') — relative, '~' is a literal dir.
    # Fixed: Path expanded to $HOME, absolute under user home.
    reproduced = (
        db is not None
        and not db.is_absolute()
        and str(db).startswith("~")
    )
    return report(
        "R3-C-05",
        reproduced=reproduced,
        evidence=(
            f"--db parsed as {db!r}; is_absolute={db.is_absolute() if db else None}, "
            f"str.startswith('~')={str(db).startswith('~') if db else None}"
        ),
    )


if __name__ == "__main__":
    sys.exit(main())
