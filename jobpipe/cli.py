"""Command-line entry point:  python -m jobpipe <command> [options]

Commands
    run     search JobsPipe -> filter -> AI assess -> append to Google Sheet
    fetch   search JobsPipe and print the normalised results (no filtering)
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config

_PROG = "python -m jobpipe"


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--config",
        default="config.yaml",
        metavar="PATH",
        help="path to the config file (default: config.yaml)",
    )
    p.add_argument(
        "--source-mode",
        choices=("sandbox", "live"),
        help="override jobspipe.mode for this run",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_PROG, description="Personal job-search pipeline.")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    run_p = sub.add_parser("run", help="full pipeline: search -> filter -> AI -> Google Sheet")
    _add_common(run_p)
    run_p.add_argument(
        "--check-config",
        action="store_true",
        help="validate the config, print the resolved settings, then exit",
    )
    run_p.add_argument(
        "--dry-run",
        action="store_true",
        help="run every stage but do not write to the Google Sheet",
    )
    run_p.add_argument(
        "--use-cached-fetch",
        action="store_true",
        help="skip JobsPipe; reuse the last run's fetched jobs (data/last_fetch.json)",
    )
    run_p.add_argument(
        "--use-cached-scores",
        action="store_true",
        help="skip JobsPipe AND Claude; reuse the last run's scored jobs (data/last_scored.json)",
    )
    run_p.add_argument(
        "--no-ai",
        action="store_true",
        help="skip the Claude calls; label all survivors 'Low Match' (for filter/sheet testing)",
    )

    fetch_p = sub.add_parser("fetch", help="search JobsPipe and print normalised results")
    _add_common(fetch_p)
    fetch_p.add_argument(
        "--limit", type=int, default=None, metavar="N", help="print at most N jobs"
    )

    return parser


def _load(args: argparse.Namespace):
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        raise SystemExit(2)
    if getattr(args, "source_mode", None):
        cfg.jobspipe.mode = args.source_mode
    return cfg


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if args.check_config:
        print(cfg.summary())
        warnings = cfg.warnings()
        if warnings:
            print("\nwarnings:")
            for w in warnings:
                print(f"  - {w}")
        else:
            print("\nno warnings.")
        return 0

    from .pipeline import run_pipeline

    return run_pipeline(
        cfg,
        dry_run=args.dry_run,
        use_cached_fetch=args.use_cached_fetch,
        use_cached_scores=args.use_cached_scores,
        no_ai=args.no_ai,
    )


def _cmd_fetch(args: argparse.Namespace) -> int:
    cfg = _load(args)
    from .pipeline import run_fetch

    return run_fetch(cfg, limit=args.limit)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # tolerate cp1252 consoles
        except (AttributeError, ValueError):
            pass

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        return _cmd_run(args)
    if args.command == "fetch":
        return _cmd_fetch(args)

    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
