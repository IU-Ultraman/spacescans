"""CLI entry: `spacescans <subcommand>`."""
from __future__ import annotations
import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spacescans",
        description="Run SpaceScans pipeline configs (C3 weights / C4 patient TWA).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<subcommand>")

    # `run`
    p_run = sub.add_parser("run", help="Run a pipeline config")
    p_run.add_argument("config", help="Path to a pipeline YAML config")
    p_run.add_argument("--data-dir", help="Override data root (highest priority)")
    p_run.add_argument("--output-dir", help="Override output root")

    # `quickstart` (filled in Task 11)
    p_qs = sub.add_parser("quickstart", help="Run an end-to-end demo on bundled sample data")
    p_qs.add_argument("--output-dir", help="Where to write demo outputs")

    # `init-config` (filled in Task 10)
    p_ic = sub.add_parser("init-config", help="Copy template configs to a directory")
    p_ic.add_argument("--out", required=True, help="Target directory for templates")

    # `data` (spacescans.data: status / fetch / verify; standard library only)
    from spacescans.data.cli import add_data_parser
    add_data_parser(sub)

    args = parser.parse_args(argv)
    if args.cmd == "run":
        from spacescans.cli._run import cmd_run
        return cmd_run(args)
    if args.cmd == "quickstart":
        from spacescans.cli._quickstart import cmd_quickstart
        return cmd_quickstart(args)
    if args.cmd == "init-config":
        from spacescans.cli._init_config import cmd_init_config
        return cmd_init_config(args)
    if args.cmd == "data":
        from spacescans.data.cli import cmd_data
        return cmd_data(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
