"""Top-level CLI router for pm-agent.

Subcommands:
  pm-agent                       Legacy TUI (back-compat default).
  pm-agent tui [args]            Explicit TUI invocation; passes args through.
  pm-agent loop run [opts]       Start the autonomous-loop daemon.
  pm-agent loop status           Print current running cycle, if any.
  pm-agent loop trigger-now      Dev helper (no-op in this build; see Task 11).
  pm-agent dashboard serve [opts] Start the FastAPI + HTMX dashboard.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def cmd_loop_run(args: argparse.Namespace) -> int:
    from pm_agent.loop import run_forever, LoopConfig
    cfg = LoopConfig(
        interval_s=args.interval_s,
        max_retries=args.max_retries,
        coder_timeout=args.coder_timeout,
        test_timeout=args.test_timeout,
    )
    try:
        asyncio.run(run_forever(args.repo, cfg))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    return 0


def cmd_loop_status(args: argparse.Namespace) -> int:
    from pm_agent import persistence
    state_db = Path.home() / ".pm-agent" / "state.db"
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
    # Re-build sys.argv so tui's own argparse sees only its args
    sys.argv = ["pm-agent"] + list(args.tui_args or [])
    tui_main()
    return 0


def _build_parser() -> argparse.ArgumentParser:
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

    return ap


def main() -> int:
    ap = _build_parser()
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
        ap.print_help()
        return 2
    if args.cmd == "dashboard":
        if args.dash_cmd == "serve":
            return cmd_dashboard_serve(args)
        ap.print_help()
        return 2
    if args.cmd == "tui":
        return cmd_tui(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
