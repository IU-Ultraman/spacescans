"""``spacescans data status|fetch|verify`` (wired into ``spacescans.cli``).

Exit codes: 0 = everything requested is usable / the fetch completed / no
checksum failed; 1 = not ready, or a handled error (message and hint on
stderr); 2 = usage error. ``--json`` prints the report as one JSON document
on stdout; progress goes to stderr either way.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Sequence

from spacescans.data.errors import DataError, UsageError


def add_data_parser(sub: Any) -> None:
    p = sub.add_parser("data", help="Download and check exposure data (status, fetch, verify)",
                       description="Download the project-mirrored exposure data into a data "
                                   "directory and check what is there. No data ships with spacescans.")
    dsub = p.add_subparsers(dest="data_cmd", required=True, metavar="<command>")

    def common(q: argparse.ArgumentParser) -> None:
        q.add_argument("variables", nargs="*", metavar="VARIABLE",
                       help="catalog variables (default: all)")
        q.add_argument("--dataset", action="append", default=[], metavar="DATASET",
                       help="a dataset by name (repeatable)")
        q.add_argument("--data-dir", help="data directory (default: $SPACESCANS_DATA_DIR)")
        q.add_argument("--json", action="store_true", help="print the report as JSON")

    q = dsub.add_parser("status", help="What is present, missing, and what to do next")
    common(q)
    q.add_argument("--deep", action="store_true",
                   help="also hash every file with a recorded sha256 (reads the whole tree)")

    q = dsub.add_parser("fetch", help="Download and place mirrored data (resumable)")
    common(q)
    q.add_argument("--accept-terms", action="append", default=[], metavar="ID",
                   help="accept a provider's terms for this call only (repeatable)")
    q.add_argument("--keep-archives", action="store_true",
                   help="keep downloaded archives under <data-dir>/.spacescans/archives/")
    q.add_argument("--dry-run", action="store_true", help="show the plan; download nothing")
    q.add_argument("--mirror", metavar="URL_OR_DIR",
                   help="download from this mirror (URL or local folder) instead of the "
                        "manifest's (default: $SPACESCANS_DATA_MIRROR)")
    q.add_argument("--force", action="store_true",
                   help="re-download mirror data even when it is already usable")

    q = dsub.add_parser("verify", help="Check sha256 of placed data (and of archive files)")
    common(q)
    q.add_argument("--quick", action="store_true", help="compare sizes only, no hashing")
    q.add_argument("--archive", action="append", default=[], metavar="PATH",
                   help="also check this downloaded archive against the manifest (repeatable)")


def cmd_data(args: argparse.Namespace) -> int:
    try:
        if args.data_cmd == "status":
            return _status(args)
        if args.data_cmd == "fetch":
            return _fetch(args)
        if args.data_cmd == "verify":
            return _verify(args)
    except UsageError as e:
        _print_error(e)
        return 2
    except DataError as e:
        _print_error(e)
        report = getattr(e, "report", None)
        if args.json:
            out = {"ok": False, "error": e.to_dict()}
            if report is not None:
                out["report"] = report.to_dict()
            print(json.dumps(out, indent=1))
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted; partial downloads are kept and resume on the next fetch",
              file=sys.stderr)
        return 130
    return 2  # pragma: no cover - argparse requires a command


def _print_error(e: DataError) -> None:
    print(f"error: {e.message}", file=sys.stderr)
    if e.hint:
        print(f"hint: {e.hint}", file=sys.stderr)


def _gb(n: int | None) -> str:
    if n is None:
        return "?"
    if n == 0:
        return "-"
    if n < 1e6:
        return f"{n / 1e3:.0f} kB"
    if n < 1e9:
        return f"{n / 1e6:.0f} MB"
    return f"{n / 1e9:.2f} GB"


def _with_dir(cmd: str, args: argparse.Namespace) -> str:
    if args.data_dir and cmd.startswith("spacescans data "):
        head, _, tail = cmd.partition(";")
        return head + f" --data-dir {args.data_dir}" + (";" + tail if tail else "")
    return cmd


def _sel(args: argparse.Namespace) -> dict[str, Any]:
    return {"variables": args.variables or None, "datasets": args.dataset or None,
            "data_dir": args.data_dir}


def _status(args: argparse.Namespace) -> int:
    from spacescans.data._status import status

    sel = _sel(args)
    rep = status(sel["variables"], datasets=sel["datasets"], data_dir=sel["data_dir"],
                 deep=args.deep, on_event=_progress_printer(quiet=False) if args.deep else None)
    if args.json:
        print(json.dumps(rep.to_dict(), indent=1))
        return 0 if rep.ready else 1
    _print_status(rep, args)
    return 0 if rep.ready else 1


def _print_status(rep: Any, args: argparse.Namespace) -> None:
    print(f"data directory: {rep.data_dir}  (data release {rep.data_release}; "
          f"free {_gb(rep.free_bytes)})")
    for w in rep.warnings:
        print(f"warning: {w}")
    if rep.variables:
        print()
        print(f"{'variable':<16} {'status':<22} {'download':>9} {'disk':>9}  next")
        for v in rep.variables:
            print(f"{v.variable:<16} {v.status:<22} {_gb(v.download_bytes):>9} "
                  f"{_gb(v.disk_bytes):>9}  {_with_dir(v.next, args)}")
    shown = [d for d in rep.datasets if not d.usable or d.dataset in rep.requested_datasets]
    if shown:
        print()
        print(f"{'dataset':<20} {'status':<22} {'present':>15}  source / licence")
        for d in shown:
            present = f"{d.present:,}/{d.expected:,}"
            print(f"{d.dataset:<20} {d.status:<22} {present:>15}  {d.provider}; {d.license}")
            for m in d.missing[:3]:
                print(f"{'':<20}   missing: {m}")
            if d.missing_count > 3:
                print(f"{'':<20}   ... {d.missing_count - 3:,} more missing")
            for prob in d.problems[:3]:
                print(f"{'':<20}   problem: {prob}")
            for w in d.warnings[:2]:
                print(f"{'':<20}   warning: {w}")
    if rep.next_steps:
        print()
        print(f"to do (about {_gb(rep.download_bytes)} to download, "
              f"{_gb(rep.disk_bytes)} on disk when complete):")
        for step in rep.next_steps:
            if step["action"] == "fetch":
                print(f"  {_with_dir(step['command'], args)}")
            else:
                print(f"  {step['dataset']} ({step['status']}):")
                if step.get("terms"):
                    t = step["terms"]
                    print(f"    terms: {t['summary']} ({t['url']})")
                for line in step["instructions"]:
                    print(f"    - {line}")
    elif rep.ready:
        print("\nall requested data is in place.")


def _progress_printer(*, quiet: bool, as_json: bool = False):
    tty = sys.stderr.isatty()
    state = {"last": 0.0, "art": None}

    def on_event(ev: dict) -> None:
        if as_json:
            print(json.dumps(ev), file=sys.stderr, flush=True)
            return
        if quiet:
            return
        kind = ev.get("ev")
        if kind == "download":
            now = time.monotonic()
            done, total = ev["bytes"], ev["total"]
            if not tty and now - state["last"] < 30 and done < total:
                return
            state["last"] = now
            pct = 100.0 * done / total if total else 100.0
            msg = (f"  {ev['artifact']}: {pct:5.1f}%  {_gb(done)} of {_gb(total)}  "
                   f"{ev.get('rate', 0) / 1e6:.1f} MB/s")
            print(("\r" + msg) if tty else msg, end="" if tty and done < total else "\n",
                  file=sys.stderr, flush=True)
        elif kind == "artifact_start":
            resume = f", resuming at {_gb(ev['resume_from'])}" if ev.get("resume_from") else ""
            print(f"[{ev['i'] + 1}/{ev['n']}] {ev['artifact']} ({_gb(ev['bytes'])}{resume})",
                  file=sys.stderr, flush=True)
        elif kind == "retry":
            print(f"\n  retry {ev['attempt']} in {ev['delay']:.0f} s: {ev['reason']}",
                  file=sys.stderr, flush=True)
        elif kind == "range_ignored":
            print(f"\n  the server ignored the resume request; restarting {ev['artifact']}",
                  file=sys.stderr, flush=True)
        elif kind == "extract":
            print(f"  extracting {ev['artifact']} ...", file=sys.stderr, flush=True)
        elif kind == "copy":
            print(f"  copying from {ev['from']}", file=sys.stderr, flush=True)
        elif kind == "artifact_end":
            if ev.get("ok"):
                print(f"  done ({ev.get('outcome')}, {ev.get('seconds', 0):.1f} s)",
                      file=sys.stderr, flush=True)
        elif kind == "verify":
            print(f"  hashing {ev['file']} ({_gb(ev['bytes'])})", file=sys.stderr, flush=True)

    return on_event


def _fetch(args: argparse.Namespace) -> int:
    from spacescans.data._fetch import fetch

    sel = _sel(args)
    kwargs = dict(datasets=sel["datasets"], data_dir=sel["data_dir"],
                  accept_terms=list(args.accept_terms), keep_archives=args.keep_archives,
                  dry_run=args.dry_run, mirror=args.mirror, force=args.force,
                  on_event=_progress_printer(quiet=False, as_json=args.json))
    rep = fetch(sel["variables"], **kwargs)
    if args.json:
        print(json.dumps(rep.to_dict(), indent=1))
        return 0 if rep.ok else 1
    p = rep.plan
    if args.dry_run:
        print(f"plan for {p.data_dir}:")
        if not p.items:
            print("  nothing to download")
        for it in p.items:
            resume = f" (resume at {_gb(it.resume_from)})" if it.resume_from else ""
            print(f"  {it.artifact:<48} {_gb(it.remaining_bytes):>9}{resume}  for {', '.join(it.datasets)}")
        print(f"  download {_gb(p.download_bytes)}; needs about {_gb(p.disk_bytes)} free "
              f"(+1 GB headroom); free now {_gb(p.free_bytes)}")
        if p.already_usable:
            print(f"  already in place: {', '.join(p.already_usable)}")
    _print_manual(p.manual)
    if not args.dry_run and rep.status is not None:
        if rep.status.ready:
            print("all requested data is in place.")
        else:
            not_ready = [f"{v.variable} ({v.status})" for v in rep.status.variables if not v.usable]
            if not_ready:
                print("still not usable: " + ", ".join(not_ready))
    return 0 if rep.ok else 1


def _print_manual(manual: Sequence[dict]) -> None:
    if not manual:
        return
    print("needs a manual step (spacescans does not download these for you):")
    for m in manual:
        print(f"  {m['dataset']} ({m['status']}): {m['title']}")
        if m.get("terms"):
            t = m["terms"]
            print(f"    terms: {t['summary']} ({t['url']})")
        for line in m["instructions"]:
            print(f"    - {line}")


def _verify(args: argparse.Namespace) -> int:
    from spacescans.data._verify import verify

    sel = _sel(args)
    rep = verify(sel["variables"], datasets=sel["datasets"], data_dir=sel["data_dir"],
                 deep=not args.quick, archives=args.archive,
                 on_event=None if args.json else _progress_printer(quiet=False))
    if args.json:
        print(json.dumps(rep.to_dict(), indent=1))
        return 0 if rep.ok else 1
    print(f"data directory: {rep.status.data_dir}")
    for a in rep.artifacts():
        label = {"verified": "ok (sha256)", "placed": "ok (sizes)", "present": "present, no receipt",
                 "missing": "missing", "partial": "partial", "corrupt": "MISMATCH"}.get(a["state"], a["state"])
        print(f"  {a['name']:<60} {label}")
        for prob in a["problems"][:5]:
            print(f"      {prob}")
    for c in rep.archives:
        print(f"  archive {c.path}: {'ok' if c.ok else 'FAILED'} ({c.detail})")
    print("verify: ok" if rep.ok else "verify: FAILED (" + ", ".join(rep.corrupt) + ")")
    return 0 if rep.ok else 1
