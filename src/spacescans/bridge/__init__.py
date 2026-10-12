"""JSON-file protocol for out-of-process clients: the R package now, later the web.

    python -I -u -X utf8 -m spacescans.bridge <command> --request req.json --result res.json

* The request file holds ``{"protocol": 1, "command", "client", "args", "verbose"}``.
* The result file always gets one envelope: ``{"protocol", "ok", "command",
  "spacescans": {"version", "code_fp", ...}, "elapsed_s", "value", "warnings", "error"}``.
* stdout carries only JSON-lines events (``{"ev": ..., "t": ...}``); everything a
  human should read goes to stderr.
* Exit codes: 0 ok, 1 handled error (result written, ``ok`` false), 2 protocol or
  usage error, 130/143 interrupted by SIGINT/SIGTERM (result written when
  possible); anything else is a crash.

This package imports with the base install only; commands import what they need.
The full contract is in docs/bridge-protocol.md in the source repository.
"""
from spacescans.bridge.protocol import PROTOCOL, PROTOCOL_RANGE

__all__ = ["PROTOCOL", "PROTOCOL_RANGE"]
