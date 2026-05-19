"""Top-level CLI router for pm-agent.

Subcommands:
  pm-agent                       Legacy TUI (back-compat default).
  pm-agent demo                  Zero-arg canonical /health demo (resets target).
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


# ---- argparse type validators (R3-A-01 / R3-C-07) ----
# Silently accepting ≤0 lets `--interval-s 0` hot-loop the API endlessly
# until the budget is exhausted. The minima below are chosen to prevent
# that class of mistake while still allowing aggressive dev configs.

def _interval_s_type(raw: str) -> int:
    try:
        v = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--interval-s must be an integer, got {raw!r}",
        ) from exc
    if v < 60:
        raise argparse.ArgumentTypeError(
            f"--interval-s must be >= 60 (1 minute); got {v}. "
            "Smaller intervals hot-loop the API and exhaust budget.",
        )
    return v


def _coder_timeout_type(raw: str) -> float:
    try:
        v = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--coder-timeout must be a number, got {raw!r}",
        ) from exc
    if v < 30:
        raise argparse.ArgumentTypeError(
            f"--coder-timeout must be >= 30 seconds; got {v}",
        )
    return v


def _test_timeout_type(raw: str) -> float:
    try:
        v = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"--test-timeout must be a number, got {raw!r}",
        ) from exc
    if v < 30:
        raise argparse.ArgumentTypeError(
            f"--test-timeout must be >= 30 seconds; got {v}",
        )
    return v


def _nonneg_int_type(raw: str) -> int:
    try:
        v = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"value must be an integer, got {raw!r}",
        ) from exc
    if v < 0:
        raise argparse.ArgumentTypeError(
            f"value must be >= 0 (0 = never retry); got {v}",
        )
    return v


def _path_expanduser(raw: str) -> Path:
    """Path argument that expands ~ to $HOME (R3-C-05).

    Plain `type=Path` happily produces Path('~/foo'), which later code
    treats as a literal directory in CWD.
    """
    return Path(raw).expanduser()


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
    # R3-C-04: only return 130 when the user actually interrupted us. Clean
    # shutdown (run_forever's signal handler set stop_event and the loop
    # exited normally) and GhAuthError-driven termination both count as
    # graceful → exit 0.
    try:
        asyncio.run(run_forever(args.repo, cfg))
    except KeyboardInterrupt:
        # Some platforms / older asyncio: outer KeyboardInterrupt fires
        # instead of run_forever's signal handler. Cover both paths.
        print("interrupted", file=sys.stderr)
        return 130
    return 0


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
    from pm_agent import persistence
    from pm_agent.loop import STATE_DB
    # R3-C-06: When the dashboard runs as a separate process from the daemon
    # (the documented "open the dashboard in another terminal" flow), its
    # persistence module never had init_db() called, so get_conn() raises
    # RuntimeError on every request. The endpoint masks that with
    # `except RuntimeError → status='idle'`, so the dashboard reads "idle"
    # forever even while the daemon happily writes cycles. init_db is
    # idempotent — safe to call even if the daemon has already initialised it.
    persistence.init_db(STATE_DB)
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


def cmd_demo(args: argparse.Namespace) -> int:
    """Zero-arg canonical /health demo: reset target + run e2e via TUI."""
    from pm_agent import demo
    from pm_agent.tui import main as tui_main

    target = demo.setup_demo_target()
    print(f"[demo] target ready at {target}")

    old_argv = sys.argv
    sys.argv = demo.build_demo_tui_argv(target)
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
    p_loop_run.add_argument("--repo", type=_path_expanduser, default=Path.cwd())
    p_loop_run.add_argument("--interval-s", type=_interval_s_type, default=1800,
                            help="cycle interval in seconds (min 60, default: 1800)")
    p_loop_run.add_argument("--max-retries", type=_nonneg_int_type, default=2,
                            help="retries per finding (0 = never retry, default: 2)")
    p_loop_run.add_argument("--coder-timeout", type=_coder_timeout_type, default=36000.0,
                            help="coder subprocess timeout in seconds (min 30, default: 36000)")
    p_loop_run.add_argument("--test-timeout", type=_test_timeout_type, default=120.0,
                            help="test subprocess timeout in seconds (min 30, default: 120)")
    sub_loop.add_parser("status", help="show current running cycle")
    p_preflight = sub_loop.add_parser(
        "preflight", help="30s readiness check before a dry run"
    )
    p_preflight.add_argument(
        "--repo", type=_path_expanduser, default=Path.cwd(),
        help="target git repo to check (default: cwd)",
    )
    p_report = sub_loop.add_parser(
        "report", help="summarise state.db after a dry run"
    )
    p_report.add_argument(
        "--db", type=_path_expanduser, default=None,
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

    # demo
    sub.add_parser(
        "demo",
        help="zero-arg canonical /health demo (resets /tmp/pm-agent-day7-target)",
    )

    return ap, p_loop, p_dash


def main() -> int:
    # Short-circuit `pm-agent tui ...` BEFORE argparse runs.
    # argparse.REMAINDER does not reliably capture `--flag` tokens at a
    # subparser boundary — the top-level parser tries to interpret them
    # and fails. Manually slurp everything after "tui" instead.
    argv = sys.argv[1:]
    if argv and argv[0] == "tui":
        from pm_agent.tui import main as tui_main
        old_argv = sys.argv
        sys.argv = ["pm-agent"] + argv[1:]
        try:
            tui_main()
        finally:
            sys.argv = old_argv
        return 0

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
    if args.cmd == "demo":
        return cmd_demo(args)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
