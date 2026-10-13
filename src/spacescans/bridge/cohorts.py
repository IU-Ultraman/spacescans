"""Reading the cohort a client hands over (``coverage``, ``link``).

The R package writes ``cohort.parquet`` with the five required columns
(pid as text, dates as Date). A Python client may pass a CSV instead. Either
way the table is checked with :func:`spacescans.cohort.validate` first, so
every problem comes back as a ``cohort`` error naming row numbers, never
values, and only the five required columns go further.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spacescans.bridge.errors import BridgeError


@dataclass
class Cohort:
    table: Any                 # pandas.DataFrame: pid, startDate, endDate, longitude, latitude
    rows: int
    patients: int
    warnings: list[dict]       # validate() warnings (row numbers and counts only)
    source: str                # "parquet" | "csv"


def read_cohort(path: str) -> Cohort:
    import pandas as pd

    from spacescans import cohort as _cohort

    p = Path(path)
    if not p.is_file():
        raise BridgeError("config", f"cohort file not found: {path}", details={"path": path})
    suffix = p.suffix.lower()
    if suffix == ".parquet":
        df = pd.read_parquet(p)
        source = "parquet"
    elif suffix in (".csv", ".txt"):
        df = _cohort.read_upload_csv(p, variant="zcta5", pid_as_str=True)
        source = "csv"
    else:
        raise BridgeError("config", f"the cohort must be a .parquet or .csv file, got {p.name!r}",
                          details={"path": path})
    report = _cohort.validate(df)
    report.raise_for_errors()          # CohortError (kind "cohort"), rows not values
    alias = _cohort.find_id_alias([str(c) for c in df.columns])
    if alias is not None:
        df = df.rename(columns={alias: "pid"})
    table = df[list(_cohort.REQUIRED)].copy()
    warnings = []
    for issue in report.warnings:
        if issue.code == "episode_id_overwritten":
            continue  # the bridge never passes the client's episode_id on
        warnings.append({"code": f"cohort_{issue.code}", "message": issue.message,
                         "column": issue.column, "rows": list(issue.rows), "count": issue.count})
    return Cohort(table=table, rows=report.n_rows, patients=int(report.n_patients or 0),
                  warnings=warnings, source=source)
