"""Cohort intake: upload checks, date parsing, and the pipeline's input.parquet.

This is the one place that decides how a residential-history cohort is read
and handed to the pipeline. The web app (spacescans-web) and the R package
both call it, so a cohort that passes here is the cohort that runs.

Ported from spacescans-web ``backend/app`` at commit 3991c3b:

* :func:`detect_date_format` and :func:`parse_date_columns` are
  ``cohort_dates.py`` verbatim (whole-column match, one format shared by
  startDate and endDate, blanks become NaT).
* :func:`check_upload` is the validation half of
  ``task_manager.save_upload`` (lines 390-470): the PATID alias, the
  required columns, and the date check and range, with the same messages.
* :func:`read_upload_csv` and :func:`to_pipeline_parquet` are
  ``csv_to_parquet`` from ``experiments/bg_ndi_wi.py:84-119`` and its
  ``experiments/zcta5_cbp.py:90-112`` variant. Called the way the web calls
  them (a CSV path, ``pid_as_str=False``), they write input.parquet with the
  same columns, dtypes and bytes, which the legacy C3 cache key depends on
  (its first 8 hex characters are the sha256 of these bytes).

:func:`validate` is new. It checks a cohort table more strictly than the web
upload does (missing ids and coordinates, coordinate ranges, date order) and
reports offending row numbers. The web keeps its own, looser rules through
:func:`check_upload`, so its behaviour does not change.

Only pandas is needed; nothing here imports the geo stack.
"""
from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Union

import pandas as pd

from spacescans.catalog.recipes import COHORT_STR_COLUMNS as _COHORT_STR_COLUMNS

_log = logging.getLogger(__name__)

__all__ = [
    "ACCEPTED_DATE_EXAMPLES",
    "ACCEPTED_DATE_FORMATS",
    "DATE_COLUMNS",
    "FIPS_STR_COLS",
    "REQUIRED",
    "VARIANTS",
    "CohortError",
    "CohortInfo",
    "CohortReport",
    "Issue",
    "UploadCheck",
    "check_upload",
    "csv_to_parquet",
    "detect_date_format",
    "find_id_alias",
    "parse_date_columns",
    "read_upload_csv",
    "to_pipeline_parquet",
    "validate",
]

#: Columns every cohort must carry (after the PATID alias is applied).
REQUIRED: tuple[str, ...] = ("pid", "startDate", "endDate", "longitude", "latitude")
#: The two date columns. They are always resolved together, with one format.
DATE_COLUMNS: tuple[str, ...] = ("startDate", "endDate")

# Tried in order; the first format that parses every value across every date
# column wins. ISO first (the canonical form), then the US slash form.
ACCEPTED_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",             # 2017-08-19  (canonical)
    "%m/%d/%Y",             # 8/19/2017   (US, month first)
)
# Same list in the form a user writes it, for error messages and the UI.
ACCEPTED_DATE_EXAMPLES = "2017-08-19 (ISO, preferred) or 8/19/2017"

# Optional cohort FIPS/GEOID columns. Nothing links on them: every variable
# derives its own spatial unit from longitude/latitude, so they are carried
# through untouched. They are read as text so leading zeros ("06" for CA)
# survive. The web has two variants: bg_ndi_wi.py:81 (four columns) and
# zcta5_cbp.py:90, which adds zcta5 and which every runner except bg_ndi_wi
# uses. The table is the catalog's (each recipe names its variant), kept once.
FIPS_STR_COLS: Mapping[str, tuple[str, ...]] = _COHORT_STR_COLUMNS
VARIANTS: tuple[str, ...] = tuple(FIPS_STR_COLS)
Variant = Literal["bg_ndi_wi", "zcta5"]

# The two runners log slightly different text when they overwrite a
# user-supplied episode_id column; kept per variant.
_EPISODE_ID_WARNING = {
    "bg_ndi_wi": (
        "input.csv carried an episode_id column; overwriting with "
        "deterministic row-index ids (Sprint 2 invariant)."
    ),
    "zcta5": (
        "input.csv carried an episode_id column; overwriting with "
        "deterministic row-index ids."
    ),
}

_ID_HINT = " (the patient id may be named pid or PATID)"
# Rows listed in an Issue; the count always covers all of them.
_MAX_ROWS_LISTED = 20

PathLike = Union[str, "os.PathLike[str]"]


class CohortError(ValueError):
    """A cohort cannot be used as given.

    A ValueError subclass, so code that catches ValueError around the web's
    upload and conversion steps keeps working. ``code`` names the problem
    (``missing_columns``, ``date_format``, ``date_timezone``, or
    ``invalid`` from :meth:`CohortReport.raise_for_errors`); ``issues``
    holds the structured findings when they come from :func:`validate`.

    ``kind`` is the bridge's error class (``cohort``) and :meth:`to_dict`
    its error shape, which never carries a cohort value.
    """

    kind = "cohort"

    def __init__(self, message: str, issues: Sequence[Issue] = (), *,
                 code: str | None = None):
        super().__init__(message)
        self.issues: tuple[Issue, ...] = tuple(issues)
        self.code = code

    def to_dict(self) -> dict[str, Any]:
        """``{"kind", "message", "hint", "details", "type"}`` without cohort values.

        Safe to log and to send to a client. The one message that quotes a
        value (an unrecognised date format names a sample date) is replaced by
        the same text without the sample; ``details`` holds the ``code`` and
        the issues with their row numbers and counts. ``str(self)`` is
        unchanged, so the web's messages stay as they are.
        """
        issues = [{**i.as_dict(), "message": _safe_message(i.code, i.message)}
                  for i in self.issues]
        if issues:
            message = "; ".join(i["message"] for i in issues)
        else:
            message = _safe_message(self.code, str(self))
        codes = {i["code"] for i in issues} | ({self.code} if self.code else set())
        if any(i["rows"] for i in issues):
            hint = "fix the rows listed in details.issues (1-based row numbers)"
        elif codes & {"missing_columns"}:
            hint = f"the cohort needs the columns {', '.join(REQUIRED)}"
        elif codes & {"date_format", "date_timezone"}:
            hint = f"give startDate and endDate as calendar dates, for example {ACCEPTED_DATE_EXAMPLES}"
        else:
            hint = None
        return {"kind": self.kind, "message": message, "hint": hint,
                "details": {"code": self.code, "issues": issues},
                "type": type(self).__name__}


_SAFE_MESSAGES = {
    # detect_date_format's message names a sample value; this is the same text without it.
    "date_format": (
        "Unrecognized date format. Dates must use one consistent format across "
        f"startDate and endDate. Accepted: {ACCEPTED_DATE_EXAMPLES}"
    ),
}


def _safe_message(code: str | None, message: str) -> str:
    """``message`` with any quoted cohort value removed (see CohortError.to_dict)."""
    return _SAFE_MESSAGES.get(code or "", message)


# ---------------------------------------------------------------------------
# Dates (cohort_dates.py, verbatim)
# ---------------------------------------------------------------------------


def _clean(values: pd.Series) -> pd.Series:
    """Trimmed strings with blanks as NA (so empty cells stay NaT, not errors)."""
    s = values.astype("string").str.strip()
    return s.mask(s == "")


def detect_date_format(columns: Iterable[pd.Series]) -> str:
    """Return the one format that parses every non-empty value in `columns`.

    Raises CohortError (a ValueError) naming a failing sample and the
    accepted formats.
    """
    non_null = [c.dropna() for c in (_clean(col) for col in columns)]
    non_null = [c for c in non_null if not c.empty]
    if not non_null:
        return ACCEPTED_DATE_FORMATS[0]  # nothing to go on; canonical is fine

    for fmt in ACCEPTED_DATE_FORMATS:
        try:
            for col in non_null:
                pd.to_datetime(col, format=fmt, errors="raise")
        except (ValueError, TypeError):
            continue
        return fmt

    sample = str(non_null[0].iloc[0])
    raise CohortError(
        f"Unrecognized date format: {sample!r}. Dates must use one consistent "
        f"format across startDate and endDate. Accepted: "
        f"{ACCEPTED_DATE_EXAMPLES}",
        code="date_format",
    )


def parse_date_columns(
    df: pd.DataFrame, columns: Sequence[str] = DATE_COLUMNS
) -> pd.DataFrame:
    """Parse `columns` in place to datetime64 using one shared format.

    Blank cells become NaT. Raises CohortError (a ValueError) if no single
    format fits. Returns `df` (the same object), as the web version does.
    """
    _parse_dates(df, columns)
    return df


def _parse_dates(df: pd.DataFrame, columns: Sequence[str]) -> str | None:
    """parse_date_columns' body; returns the format, or None if no column."""
    present = [c for c in columns if c in df.columns]
    if not present:
        return None

    fmt = detect_date_format(df[c] for c in present)
    if fmt != ACCEPTED_DATE_FORMATS[0]:
        _log.info(
            "cohort dates parsed with format %r (columns: %s)",
            fmt, ", ".join(present),
        )
    for c in present:
        df[c] = pd.to_datetime(_clean(df[c]), format=fmt, errors="coerce")
    return fmt


# ---------------------------------------------------------------------------
# Upload check (task_manager.save_upload 390-470)
# ---------------------------------------------------------------------------


def find_id_alias(columns: Sequence[str]) -> str | None:
    """The column to rename to ``pid``, or None.

    ``PATID`` in any case is accepted as the identifier, but only when no
    ``pid`` column exists: a file carrying both keeps ``pid`` untouched.
    """
    if "pid" in columns:
        return None
    return next((c for c in columns if c.lower() == "patid"), None)


def _missing_columns_message(missing: Iterable[str]) -> str:
    missing = set(missing)
    return (
        f"Missing required columns: {', '.join(sorted(missing))}"
        + (_ID_HINT if "pid" in missing else "")
    )


@dataclass(frozen=True)
class UploadCheck:
    """What :func:`check_upload` found in an uploaded CSV.

    ``summary`` is the web task's ``data_summary`` as stored in meta.json:
    ``row_count``, ``columns`` (after the alias rename), ``date_range``
    (``{"min", "max"}`` as YYYY-MM-DD, or None each) and ``filename``.
    ``rewritten_text`` is the CSV text to store when the id column was
    renamed (only the header changes; values, dates included, are written
    back verbatim); None means keep the upload as it is.
    """

    summary: dict[str, Any]
    renamed_id_from: str | None = None
    rewritten_text: str | None = None


def check_upload(content: bytes, filename: str) -> UploadCheck:
    """Validate an uploaded cohort CSV the way the web upload does.

    Port of the checks in ``task_manager.save_upload``: decode as UTF-8,
    rename a PATID column (any case) to ``pid`` when there is no ``pid``,
    require the five columns, and parse every startDate and endDate value
    through :func:`parse_date_columns` as one column to get the date range.
    Raises CohortError (a ValueError) with the web's messages. Writing the
    file, deleting it on error and updating meta.json stay with the caller.
    """
    text = content.decode("utf-8")
    reader = csv.DictReader(text.splitlines())
    rows = list(reader)
    columns = reader.fieldnames or []

    renamed_id_from = None
    alias = find_id_alias(columns)
    if alias is not None:
        renamed_id_from = alias
        columns = ["pid" if c == alias else c for c in columns]
        for row in rows:
            row["pid"] = row.pop(alias)

    missing = set(REQUIRED) - set(columns)
    if missing:
        raise CohortError(_missing_columns_message(missing), code="missing_columns")

    date_strings = [r.get("startDate", "") for r in rows] + [
        r.get("endDate", "") for r in rows
    ]
    date_range: dict[str, str | None] = {"min": None, "max": None}
    if any(d for d in date_strings):
        parsed = parse_date_columns(
            pd.DataFrame({"startDate": date_strings}), ("startDate",)
        )["startDate"].dropna()
        if not parsed.empty:
            date_range = {
                "min": parsed.min().strftime("%Y-%m-%d"),
                "max": parsed.max().strftime("%Y-%m-%d"),
            }

    rewritten_text = None
    if renamed_id_from is not None:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        rewritten_text = buf.getvalue()
        _log.info("upload: renamed identifier column %r -> 'pid'", renamed_id_from)

    summary = {
        "row_count": len(rows),
        "columns": columns,
        "date_range": date_range,
        "filename": filename,
    }
    return UploadCheck(summary=summary, renamed_id_from=renamed_id_from,
                       rewritten_text=rewritten_text)


# ---------------------------------------------------------------------------
# Strict validation of a cohort table (new; used by link() and the R bridge)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    """One finding of :func:`validate`.

    ``rows`` are 1-based positions in the cohort table (R's row numbers; in
    a CSV with a header, row r is on line r + 1). At most 20 are listed;
    ``count`` is the total.
    """

    code: str
    message: str
    column: str | None = None
    rows: tuple[int, ...] = ()
    count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "column": self.column,
                "rows": list(self.rows), "count": self.count}


@dataclass(frozen=True)
class CohortReport:
    """Result of :func:`validate`. ``ok`` is True when there are no errors."""

    n_rows: int
    n_patients: int | None
    columns: tuple[str, ...]
    id_column: str | None
    date_format: str | None
    date_range: dict[str, str | None]
    errors: tuple[Issue, ...] = ()
    warnings: tuple[Issue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_for_errors(self) -> None:
        """Raise CohortError listing every error, or return quietly."""
        if self.errors:
            raise CohortError("; ".join(i.message for i in self.errors),
                              self.errors, code="invalid")

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "n_rows": self.n_rows, "n_patients": self.n_patients,
            "columns": list(self.columns), "id_column": self.id_column,
            "date_format": self.date_format, "date_range": dict(self.date_range),
            "errors": [i.as_dict() for i in self.errors],
            "warnings": [i.as_dict() for i in self.warnings],
        }


def _rows_issue(code: str, mask: pd.Series, what: str, column: str | None) -> Issue:
    positions = [int(i) + 1 for i in mask.to_numpy().nonzero()[0]]
    listed = positions[:_MAX_ROWS_LISTED]
    more = "" if len(positions) <= len(listed) else f" and {len(positions) - len(listed)} more"
    noun = "row" if len(positions) == 1 else "rows"
    message = f"{what} in {len(positions)} {noun} ({noun} {', '.join(map(str, listed))}{more})"
    return Issue(code=code, message=message, column=column, rows=tuple(listed),
                 count=len(positions))


def _blank(s: pd.Series) -> pd.Series:
    """True where a value is NA or a blank string."""
    out = s.isna()
    if not (pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s)):
        text = s.astype("string").str.strip()
        out = out | (text == "").fillna(False).astype(bool)
    return out.astype(bool)


def validate(df: pd.DataFrame) -> CohortReport:
    """Check a cohort table before linking. Never modifies `df`.

    Errors: no rows; missing required columns (PATID in any case is accepted
    for ``pid``); a missing ``pid``; a missing, non-numeric or out-of-range
    longitude (-180..180) or latitude (-90..90); dates in no accepted format
    (see :data:`ACCEPTED_DATE_EXAMPLES`) or timezone-aware timestamps; an
    endDate before its startDate.

    Warnings: a blank startDate or endDate (the web accepts these; such an
    episode gets no exposure values, static or time-varying, because the
    pipeline cannot build its period); a user ``episode_id`` column, which
    is replaced by the row number.

    Points outside the contiguous US are not errors here; coverage reports
    them per variable.
    """
    errors: list[Issue] = []
    warnings: list[Issue] = []
    columns = tuple(str(c) for c in df.columns)
    n = len(df)

    alias = find_id_alias(columns)
    id_column = "pid" if "pid" in columns else alias
    work = df.rename(columns={alias: "pid"}) if alias is not None else df

    missing = set(REQUIRED) - set(work.columns)
    if missing:
        errors.append(Issue("missing_columns", _missing_columns_message(missing),
                            count=len(missing)))
    if n == 0:
        errors.append(Issue("empty", "The cohort has no rows"))

    n_patients = None
    if "pid" in work.columns:
        bad = _blank(work["pid"])
        if bad.any():
            errors.append(_rows_issue("pid_missing", bad, "Missing pid", "pid"))
        n_patients = int(work["pid"][~bad].nunique())

    for col, lo, hi in (("longitude", -180, 180), ("latitude", -90, 90)):
        if col not in work.columns:
            continue
        raw = work[col]
        blank = _blank(raw)
        num = pd.to_numeric(raw, errors="coerce")
        not_num = num.isna() & ~blank
        if blank.any():
            errors.append(_rows_issue("coord_missing", blank, f"Missing {col}", col))
        if not_num.any():
            errors.append(_rows_issue("coord_not_numeric", not_num,
                                      f"Non-numeric {col}", col))
        out = num.notna() & ~num.between(lo, hi)
        if out.any():
            errors.append(_rows_issue("coord_range", out,
                                      f"{col} outside {lo}..{hi}", col))

    date_format = None
    date_range: dict[str, str | None] = {"min": None, "max": None}
    present = [c for c in DATE_COLUMNS if c in work.columns]
    if present:
        dates = pd.DataFrame({c: work[c] for c in present})
        try:
            date_format = _coerce_dates(dates)
        except CohortError as e:
            errors.append(Issue(e.code or "date_format", str(e)))
        else:
            for c in present:
                missing_date = dates[c].isna()
                if missing_date.any():
                    warnings.append(_rows_issue("date_missing", missing_date,
                                                f"Blank {c}", c))
            if len(present) == 2:
                order = dates["endDate"] < dates["startDate"]
                order = order.fillna(False).astype(bool)
                if order.any():
                    errors.append(_rows_issue("date_order", order,
                                              "endDate before startDate", None))
            stacked = pd.concat([dates[c] for c in present]).dropna()
            if not stacked.empty:
                date_range = {"min": stacked.min().strftime("%Y-%m-%d"),
                              "max": stacked.max().strftime("%Y-%m-%d")}

    if "episode_id" in work.columns:
        warnings.append(Issue(
            "episode_id_overwritten",
            "The cohort has an episode_id column; it is replaced by the row "
            "number (0-based), which is how results are joined back",
            column="episode_id"))

    return CohortReport(n_rows=n, n_patients=n_patients, columns=columns,
                        id_column=id_column, date_format=date_format,
                        date_range=date_range, errors=tuple(errors),
                        warnings=tuple(warnings))


# ---------------------------------------------------------------------------
# input.parquet (csv_to_parquet, both FIPS variants)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CohortInfo:
    """What :func:`to_pipeline_parquet` wrote.

    ``sha256`` is the digest of the parquet bytes; the legacy C3 cache key
    starts with its first 8 hex characters. ``patients`` is None when the
    table has no ``pid`` column (the legacy path does not check).
    """

    path: Path
    rows: int
    patients: int | None
    columns: tuple[str, ...]
    sha256: str
    episode_id_overwritten: bool
    date_format: str | None


def _check_variant(variant: str) -> tuple[str, ...]:
    try:
        return FIPS_STR_COLS[variant]
    except KeyError:
        raise ValueError(
            f"unknown cohort variant {variant!r} (expected one of {list(VARIANTS)})"
        ) from None


def read_upload_csv(path: PathLike, *, variant: Variant = "zcta5",
                    pid_as_str: bool = False) -> pd.DataFrame:
    """Read a cohort CSV the way the web runners do before conversion.

    The variant's FIPS columns that are present are read as ``str``; every
    other column is inferred by pandas, so a numeric ``pid`` becomes int64
    and loses leading zeros unless ``pid_as_str`` is set (then ``pid``, or
    its PATID alias, is read as text too).
    """
    fips = _check_variant(variant)
    header = pd.read_csv(path, nrows=0).columns.tolist()
    # Plain `str` (not the "string" extension dtype), as the web does.
    dtypes: dict[str, Any] = {c: str for c in fips if c in header}
    if pid_as_str:
        id_col = "pid" if "pid" in header else find_id_alias(header)
        if id_col is not None:
            dtypes[id_col] = str
    return pd.read_csv(path, dtype=dtypes)


def _coerce_dates(df: pd.DataFrame) -> str | None:
    """Parse the date columns of `df` in place; return the format used.

    Columns that are already datetime64 (as from a typed table) are kept;
    timezone-aware ones are refused. The rest go through
    :func:`parse_date_columns` together, so they share one format. Returns
    None when nothing needed parsing.
    """
    to_parse = []
    for c in DATE_COLUMNS:
        if c not in df.columns:
            continue
        dtype = df[c].dtype
        if isinstance(dtype, pd.DatetimeTZDtype):
            raise CohortError(
                f"{c} holds timezone-aware timestamps; pass calendar dates "
                f"(for example {ACCEPTED_DATE_EXAMPLES})",
                code="date_timezone",
            )
        if pd.api.types.is_datetime64_dtype(dtype):
            continue
        to_parse.append(c)
    return _parse_dates(df, to_parse)


def _pid_to_str(s: pd.Series) -> pd.Series:
    """Identifiers as text, NA kept as NA. Integral floats lose the '.0'."""
    def conv(v: Any) -> Any:
        if v is None or (not isinstance(v, str) and pd.isna(v)):
            return None
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return v if isinstance(v, str) else str(v)
    return pd.Series([conv(v) for v in s.tolist()], index=s.index, dtype=object)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def to_pipeline_parquet(
    cohort: pd.DataFrame | PathLike,
    dst: PathLike,
    *,
    variant: Variant = "zcta5",
    pid_as_str: bool,
    columns: Literal["all", "required"] = "all",
) -> CohortInfo:
    """Write the pipeline's input.parquet from a cohort CSV path or table.

    Steps, in the web's order: read (a path goes through
    :func:`read_upload_csv` with `variant`), parse startDate/endDate with
    one shared format, set ``episode_id = range(len(df))`` (overwriting any
    existing column in place, with a logged warning), and
    ``to_parquet(index=False)``. The pipeline's ``demo_conus`` adapter turns
    episode_id into the per-row geoid, and the merge step joins results
    back on it, so rows must not be sorted, dropped or deduplicated first.

    ``pid_as_str=False`` is the web's legacy behaviour: called with a CSV
    path it gives byte-identical output to the web's ``csv_to_parquet``
    (same pandas and pyarrow), which keeps legacy cache keys valid.
    ``pid_as_str=True`` (R and new callers) also applies the PATID alias,
    requires the five columns and stores ``pid`` as text, so numeric and
    zero-padded ids survive. ``columns="required"`` keeps only the five
    columns plus episode_id, so no other cohort column is written to disk.

    `variant` only affects reading a CSV; a table's dtypes are used as
    given. A table passed in is not modified.
    """
    if columns not in ("all", "required"):
        raise ValueError(f"columns must be 'all' or 'required', got {columns!r}")
    _check_variant(variant)
    if isinstance(cohort, pd.DataFrame):
        df = cohort.copy()
    else:
        df = read_upload_csv(cohort, variant=variant, pid_as_str=pid_as_str)

    if pid_as_str:
        alias = find_id_alias(list(df.columns))
        if alias is not None:
            df = df.rename(columns={alias: "pid"})
        missing = set(REQUIRED) - set(df.columns)
        if missing:
            raise CohortError(_missing_columns_message(missing),
                              code="missing_columns")
        if not (pd.api.types.is_string_dtype(df["pid"].dtype)
                and df["pid"].map(lambda v: isinstance(v, str) or pd.isna(v)).all()):
            df["pid"] = _pid_to_str(df["pid"])
    elif columns == "required":
        missing = set(REQUIRED) - set(df.columns)
        if missing:
            raise CohortError(_missing_columns_message(missing),
                              code="missing_columns")

    date_format = _coerce_dates(df)
    overwritten = "episode_id" in df.columns
    if overwritten:
        _log.warning(_EPISODE_ID_WARNING[variant])
    df["episode_id"] = range(len(df))
    if columns == "required":
        df = df[list(REQUIRED) + ["episode_id"]]

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dst, index=False)

    patients = int(df["pid"].nunique()) if "pid" in df.columns else None
    return CohortInfo(path=dst, rows=len(df), patients=patients,
                      columns=tuple(str(c) for c in df.columns),
                      sha256=_sha256(dst), episode_id_overwritten=overwritten,
                      date_format=date_format)


def csv_to_parquet(src: PathLike, dst: PathLike, *,
                   variant: Variant = "zcta5") -> CohortInfo:
    """The web runners' ``csv_to_parquet``: legacy, byte-identical output.

    ``variant="bg_ndi_wi"`` is bg_ndi_wi.py's version (zcta5 is inferred);
    ``"zcta5"`` is zcta5_cbp.py's, used by every other runner.
    """
    return to_pipeline_parquet(src, dst, variant=variant, pid_as_str=False)
