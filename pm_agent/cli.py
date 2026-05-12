"""Top-level CLI router for pm-agent.

Subcommands:
  pm-agent                       Legacy TUI (back-compat default).
  pm-agent tui [args]            Explicit TUI invocation; passes args through.
  pm-agent loop run [opts]       Start the autonomous-loop daemon.
  pm-agent loop status           Print current running cycle, if any.
  pm-agent loop preflight        30-second readiness check before a dry run.
  pm-agent loop report           Summarise state.db after a dry run.
  pm-agent loop trigger-now      Dev helper (no-op in this build; see Task 11).
  pm-agent dashboard serve [opts] Start the FastAPI + HTMX dashboard.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def cmd_loop_run(args: argparse.Namespace) -> int:
    from pm_agent.loop import run_forever, LoopConfig
    # INFO logs from loop.py give the operator visibility during the 14h run.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    cfg = LoopConfig(
        interval_s=args.interval_s,
        max_retries=args.max_retries,
        coder_timeout=args.coder_timeout,
        test_timeout=args.test_timeout,
    )
    try:
        asyncio.run(run_forever(args.repo, cfg))
    except KeyboardInterrupt:
        # Some platforms / older asyncio: outer KeyboardInterrupt fires
        # instead of run_forever's signal handler. Cover both paths.
        print("interrupted", file=sys.stderr)
        return 130
    # Normal path: run_forever's signal handler set stop_event; the daemon
    # exited cleanly. Treat any return as signal-driven (the loop has no
    # other exit besides GhAuthError which propagates).
    print("interrupted", file=sys.stderr)
    return 130


def cmd_loop_preflight(args: argparse.Namespace) -> int:
    from pm_agent.loop import STATE_DB
    from pm_agent import preflight as pf

    state_db = STATE_DB
    repo = Path(args.repo) if hasattr(args, "repo") and args.repo else Path.cwd()

    results, all_passed = pf.run_preflight(state_db, repo)
    pf.print_preflight(results, all_passed)
    return 0 if all_passed else 1


def cmd_loop_report(args: argparse.Namespace) -> int:
    from pm_agent.loop import STATE_DB
    from pm_agent import report as rpt

    if hasattr(args, "db") and args.db:
        db_path = Path(args.db)
    else:
        db_path = STATE_DB

    rpt.print_report(db_path)
    return 0


def cmd_loop_status(args: argparse.Namespace) -> int:
    from pm_agent import persistence
    from pm_agent.loop import STATE_DB
    state_db = STATE_DB
    if not state_db.exists():
        print("no running cycle (state.db not initialized)")
        return 0
    persistence.init_db(state_db)
    c = persistence.get_conn()
    row = c.execute(
        """SELECT id, status, started_at FROM cycles
           WHERE status='running' ORDER BY started_at DESC LIMIT 1""",
    ).fetchone()
    if row is None:
        print("no running cycle")
    else:
        print(f"cycle {row['id']} running since {row['started_at']}")
    return 0


def cmd_dashboard_serve(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run(
        "pm_agent.dashboard.server:app",
        host=args.host, port=args.port, reload=False,
    )
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    from pm_agent.tui import main as tui_main
    old_argv = sys.argv
    sys.argv = ["pm-agent"] + list(args.tui_args or [])
    try:
        tui_main()
    finally:
        sys.argv = old_argv
    return 0


def _build_parser() -> tuple[argparse.ArgumentParser, argparse.ArgumentParser, argparse.ArgumentParser]:
    """Return (root_parser, loop_subparser, dashboard_subparser)."""
    ap = argparse.ArgumentParser(prog="pm-agent")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    # loop
    p_loop = sub.add_parser("loop", help="autonomous loop daemon")
    sub_loop = p_loop.add_subparsers(dest="loop_cmd", metavar="LOOP_CMD")
    p_loop_run = sub_loop.add_parser("run", help="start daemon")
    p_loop_run.add_argument("--repo", type=Path, default=Path.cwd())
    p_loop_run.add_argument("--interval-s", type=int, default=1800,
                            help="cycle interval in seconds (default: 1800)")
    p_loop_run.add_argument("--max-retries", type=int, default=2)
    p_loop_run.add_argument("--coder-timeout", type=float, default=180.0)
    p_loop_run.add_argument("--test-timeout", type=float, default=120.0)
    sub_loop.add_parser("status", help="show current running cycle")
    p_preflight = sub_loop.add_parser(
        "preflight", help="30s readiness check before a dry run"
    )
    p_preflight.add_argument(
        "--repo", type=Path, default=Path.cwd(),
        help="target git repo to check (default: cwd)",
    )
    p_report = sub_loop.add_parser(
        "report", help="summarise state.db after a dry run"
    )
    p_report.add_argument(
        "--db", type=Path, default=None,
        help="path to state.db (default: ~/.pm-agent/state.db)",
    )

    # dashboard
    p_dash = sub.add_parser("dashboard", help="FastAPI web dashboard")
    sub_dash = p_dash.add_subparsers(dest="dash_cmd", metavar="DASH_CMD")
    p_serve = sub_dash.add_parser("serve", help="start uvicorn server")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--host", default="127.0.0.1")

    # tui
    p_tui = sub.add_parser("tui", help="legacy Textual TUI (default when no subcommand)")
    p_tui.add_argument("tui_args", nargs=argparse.REMAINDER,
                       help="args passed through to pm_agent.tui")

    return ap, p_loop, p_dash


def main() -> int:
    ap, p_loop, p_dash = _build_parser()
    args = ap.parse_args()
    if args.cmd is None:
        # back-compat: pm-agent with no subcommand → TUI
        from pm_agent.tui import main as tui_main
        tui_main()
        return 0
    if args.cmd == "loop":
        if args.loop_cmd == "run":
            return cmd_loop_run(args)
        if args.loop_cmd == "status":
            return cmd_loop_status(args)
        if args.loop_cmd == "preflight":
            return cmd_loop_preflight(args)
        if args.loop_cmd == "report":
            return cmd_loop_report(args)
        p_loop.print_help()  # bare 'pm-agent loop' shows loop's help
        return 2
    if args.cmd == "dashboard":
        if args.dash_cmd == "serve":
            return cmd_dashboard_serve(args)
        p_dash.print_help()  # bare 'pm-agent dashboard' shows dashboard's help
        return 2
    if args.cmd == "tui":
        return cmd_tui(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
