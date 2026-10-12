"""``data-*`` commands: thin wrappers over :mod:`spacescans.data`.

Each maps one to one onto the Python API and returns the report's
``to_dict()``. The data errors carry their own kinds (``data_missing``,
``data_corrupt``, ``network``, ``disk_space``, ``locked``,
``terms_required``, ``config``), which the bridge keeps. ``data-fetch`` and
``data-verify`` relay the module's events (``artifact_start``, ``download``,
``verify``, ``extract_progress`` ...) as bridge events; the data module
already limits ``download`` events to 4 per second.

Common arguments: ``variables`` and/or ``datasets`` (lists; a single string
is a list of one), ``data_dir`` (absolute; else ``SPACESCANS_DATA_DIR``),
``manifest`` (absolute path of another manifest, for tests and private
mirrors), ``mirror`` (a URL or an absolute folder, for fetch and plan).
"""
from __future__ import annotations

from typing import Any

from spacescans.bridge.args import (
    bool_arg,
    path_arg,
    path_list_arg,
    str_arg,
    str_list_arg,
)
from spacescans.bridge.errors import BridgeError
from spacescans.bridge.worker import run_captured, warning_records

Outcome = tuple[Any, list]


def _selection(args: dict) -> dict[str, Any]:
    return {"variables": str_list_arg(args, "variables"),
            "datasets": str_list_arg(args, "datasets"),
            "data_dir": path_arg(args, "data_dir"),
            "manifest": path_arg(args, "manifest")}


def _mirror(args: dict) -> str | None:
    mirror = str_arg(args, "mirror")
    if mirror is not None and not mirror.startswith(("http://", "https://", "file://")):
        path_arg(args, "mirror")  # a folder: must be absolute
    return mirror


def _relay(ctx):
    def on_event(ev: dict) -> None:
        if not isinstance(ev, dict):
            return
        fields = {k: v for k, v in ev.items() if k not in ("ev", "t")}
        ctx.events.emit(str(ev.get("ev") or "data"), **fields)
    return on_event


def _report_warnings(report: dict) -> list[dict]:
    status = report.get("status") if "variables" not in report else report
    texts = (status or {}).get("warnings") or []
    return [{"code": "data_warning", "message": str(t)} for t in texts]


def _call(fn, ctx) -> tuple[Any, list]:
    """Run ``fn`` on a worker thread; stray output goes to the human log."""
    def echo(line: str) -> None:
        ctx.log(line)
    value, caught = run_captured(fn, echo)
    return value, warning_records(caught)


def cmd_data_status(args: dict, ctx) -> Outcome:
    """``data.status``: per variable and dataset, what is there and what to do next."""
    from spacescans import data
    sel = _selection(args)
    deep = bool_arg(args, "deep")
    rep, warns = _call(lambda: data.status(sel["variables"], datasets=sel["datasets"],
                                           data_dir=sel["data_dir"], deep=deep,
                                           manifest=sel["manifest"],
                                           on_event=_relay(ctx) if deep else None), ctx)
    value = rep.to_dict()
    return value, _report_warnings(value) + warns


def cmd_data_plan(args: dict, ctx) -> Outcome:
    """``data.plan``: what ``data-fetch`` would download (shared artifacts once). No network."""
    from spacescans import data
    sel = _selection(args)
    plan, warns = _call(lambda: data.plan(
        sel["variables"], datasets=sel["datasets"], data_dir=sel["data_dir"],
        accept_terms=str_list_arg(args, "accept_terms") or (),
        force=bool_arg(args, "force"), keep_archives=bool_arg(args, "keep_archives"),
        mirror=_mirror(args), manifest=sel["manifest"]), ctx)
    return plan.to_dict(), warns


def cmd_data_verify(args: dict, ctx) -> Outcome:
    """``data.verify``: hash every placed file with a recorded sha256 (``deep`` default true)."""
    from spacescans import data
    sel = _selection(args)
    rep, warns = _call(lambda: data.verify(
        sel["variables"], datasets=sel["datasets"], data_dir=sel["data_dir"],
        deep=bool_arg(args, "deep", True), archives=path_list_arg(args, "archives") or (),
        on_event=_relay(ctx), manifest=sel["manifest"]), ctx)
    value = rep.to_dict()
    return value, _report_warnings(value) + warns


def cmd_data_fetch(args: dict, ctx) -> Outcome:
    """``data.fetch``: resumable mirror downloads. Terms are accepted per call only.

    Interrupting keeps the ``.part`` files; the next fetch resumes them.
    """
    from spacescans import data
    sel = _selection(args)
    rep, warns = _call(lambda: data.fetch(
        sel["variables"], datasets=sel["datasets"], data_dir=sel["data_dir"],
        accept_terms=str_list_arg(args, "accept_terms") or (),
        keep_archives=bool_arg(args, "keep_archives"), dry_run=bool_arg(args, "dry_run"),
        mirror=_mirror(args), force=bool_arg(args, "force"), on_event=_relay(ctx),
        manifest=sel["manifest"]), ctx)
    value = rep.to_dict()
    for item in value.get("manual") or []:
        warns.append({"code": "manual_step", "dataset": item.get("dataset"),
                      "message": f"{item.get('dataset')}: needs a manual step; see its instructions"})
    return value, _report_warnings(value) + warns


def cmd_data_import(args: dict, ctx) -> Outcome:
    """``data.import_files``: place hand-downloaded provider files where the readers look."""
    from spacescans import data
    dataset = str_arg(args, "dataset", required=True)
    paths = path_list_arg(args, "paths", required=True)
    if not paths:
        raise BridgeError("config", "argument 'paths' must list at least one file")
    rep, warns = _call(lambda: data.import_files(
        dataset, paths, data_dir=path_arg(args, "data_dir"), move=bool_arg(args, "move"),
        manifest=path_arg(args, "manifest")), ctx)
    value = rep.to_dict()
    for item in value.get("items") or []:
        if item.get("result") == "rejected":
            warns.append({"code": "import_rejected", "path": item.get("source"),
                          "message": f"{item.get('source')}: {item.get('detail')}"})
    return value, warns


def cmd_data_terms(args: dict, ctx) -> Outcome:
    """``data.terms``: licence, attribution, terms and instructions for one dataset."""
    from spacescans import data
    dataset = str_arg(args, "dataset", required=True)
    return data.terms(dataset, manifest=path_arg(args, "manifest")), []


COMMANDS = {
    "data-status": cmd_data_status,
    "data-plan": cmd_data_plan,
    "data-verify": cmd_data_verify,
    "data-fetch": cmd_data_fetch,
    "data-import": cmd_data_import,
    "data-terms": cmd_data_terms,
}
