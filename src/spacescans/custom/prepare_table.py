"""Registered polygon tables: values keyed by a 2010 Census geography.

A ``polygon_table`` dataset is one CSV (or Parquet) file with a key column of
tract, block group, ZCTA or county codes, value columns, and for a ``yearly``
dataset a year column or for a ``daily`` dataset a date column.
:func:`prepare_table` checks it with the upload path's rules (zero-padded keys
of the boundary's width, one row per key and period, numeric values) and
writes ``values.parquet``: ``geo_key`` (text), ``year`` (integer) or ``date``
(date), then one double column per variable, sorted by key and period.

DuckDB does the work, streaming the file, so a national daily table of
hundreds of millions of rows is checked and converted without being loaded
into Python; its spill files stay in the work directory. Every message gives
counts, column names and expected formats, never a value from the file.

Accepted dates: ``YYYY-MM-DD``, ``YYYY/MM/DD``, ``YYYYMMDD`` and ``MM/DD/YYYY``
(a time of day after the date is ignored); years 1900-2100. Missing values:
blank, ``NA``, ``N/A``, ``NULL``, ``NaN``, ``.`` (any case); infinite values
are stored as missing too.
"""
from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spacescans.custom.prepare import (
    Prepared,
    common_definition,
    normalise_variables,
    public_variables,
    slug,
)
from spacescans.custom.registry import DatasetError, sha256_of
from spacescans.custom.table import BOUNDARY_SPEC

if TYPE_CHECKING:  # pragma: no cover
    from spacescans.custom.registry import DatasetRecord, Input

VALUES_FILE = "values.parquet"
KEY_COLUMN = "geo_key"
_NON_NUMERIC_TOLERANCE = 0.5
_MISSING_TOKENS = ("NA", "N/A", "NULL", "NAN", ".")
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S",
                 "%Y-%m-%dT%H:%M:%S")
_TABLE_SUFFIXES = (".csv", ".txt", ".csv.gz", ".parquet")

#: Boundary names a user may type -> the BOUNDARY_SPEC key.
BOUNDARY_ALIASES: Mapping[str, str] = {
    "tract": "Tract", "tracts": "Tract", "census_tract": "Tract",
    "bg": "BG", "block_group": "BG", "blockgroup": "BG", "block_groups": "BG",
    "zcta": "ZCTA5", "zcta5": "ZCTA5", "zip": "ZCTA5", "zip_code": "ZCTA5",
    "county": "County", "counties": "County",
}
# Usual names of each boundary's key column, tried in order when none is named.
_KEY_GUESSES: Mapping[str, tuple[str, ...]] = {
    "Tract": ("GEOID10", "GEOID", "geoid10", "geoid", "FIPS", "fips"),
    "BG": ("GEOID10", "GEOID", "geoid10", "geoid", "FIPS", "fips"),
    "County": ("GEOID10", "GEOID", "geoid10", "geoid", "FIPS", "fips"),
    "ZCTA5": ("ZCTA5CE10", "ZCTA5", "zcta5", "ZCTA", "zcta", "ZIP", "zip", "GEOID10", "GEOID"),
}


def boundary_name(value: Any) -> str:
    """The canonical boundary (``Tract``, ``BG``, ``ZCTA5``, ``County``) of a user's name."""
    if isinstance(value, str):
        if value in BOUNDARY_SPEC:
            return value
        canon = BOUNDARY_ALIASES.get(value.strip().lower().replace("-", "_").replace(" ", "_"))
        if canon:
            return canon
    raise DatasetError(f"unknown boundary {value!r}; use one of 'tract', 'block_group', "
                       "'zcta' or 'county' (2010 Census geographies)")


def _q(name: str) -> str:
    """A SQL identifier, quoted."""
    return '"' + name.replace('"', '""') + '"'


def _lit(text: str) -> str:
    """A SQL string literal."""
    return "'" + text.replace("'", "''") + "'"


def _file_error(path: Path, exc: Exception) -> DatasetError:
    """A parse error without the file's content (DuckDB quotes the offending line)."""
    import re
    match = re.search(r"[Ll]ine:?\s*(\d+)", str(exc))
    where = f" (near line {match.group(1)})" if match else ""
    return DatasetError(f"{path.name} could not be read as a CSV table{where}; check that it "
                        "is comma-separated text with one header line")


def _open_table(con: Any, path: Path) -> list[str]:
    """Create the view ``raw`` (every column as text) over the file; return its columns."""
    import duckdb

    name = path.name.lower()
    if not name.endswith(_TABLE_SUFFIXES):
        raise DatasetError(f"{path.name}: a polygon table is a .csv (or .parquet) file")
    if name.endswith(".parquet"):
        try:
            con.execute(f"CREATE VIEW raw0 AS SELECT * FROM read_parquet({_lit(str(path))})")
            cols = [r[0] for r in con.execute("DESCRIBE raw0").fetchall()]
        except duckdb.Error:
            raise DatasetError(f"{path.name} could not be read as a Parquet table") from None
        casts = ", ".join(f"CAST({_q(c)} AS VARCHAR) AS {_q(c)}" for c in cols)
        con.execute(f"CREATE VIEW raw AS SELECT {casts} FROM raw0")
        return cols
    last: Exception | None = None
    for encoding in ("utf-8", "latin-1"):
        try:
            con.execute(
                f"CREATE OR REPLACE VIEW raw AS SELECT * FROM read_csv({_lit(str(path))}, "
                "all_varchar=true, header=true, delim=',', quote='\"', escape='\"', "
                f"null_padding=true, encoding={_lit(encoding)})")
            con.execute("SELECT count(*) FROM raw").fetchone()      # a full pass
            return [r[0] for r in con.execute("DESCRIBE raw").fetchall()]
        except duckdb.Error as exc:
            last = exc
            if "unicode" in str(exc).lower() or "utf" in str(exc).lower():
                continue
            raise _file_error(path, exc) from None
    raise _file_error(path, last or RuntimeError()) from None


def _find_column(header: Sequence[str], wanted: str) -> str | None:
    if wanted in header:
        return wanted
    lowered = {h.lower(): h for h in header}
    return lowered.get(wanted.lower())


def _key_spec(key: Any, header: Sequence[str], temporal: str) -> dict[str, Any]:
    """The record's ``key``: boundary, key column, and the year or date column."""
    if key is None:
        raise DatasetError("a polygon table needs key={'boundary': 'tract' | 'block_group' | "
                           "'zcta' | 'county', 'column': <key column>}")
    if isinstance(key, str):
        key = {"boundary": key}
    if not isinstance(key, Mapping):
        raise DatasetError("key must be a boundary name or a mapping with 'boundary' and "
                           "'column'")
    allowed = {"boundary", "column", "year_column", "date_column"}
    unknown = sorted(set(key) - allowed)
    if unknown:
        raise DatasetError(f"unknown key field(s): {', '.join(map(str, unknown))} "
                           f"(known: {', '.join(sorted(allowed))})")
    boundary = boundary_name(key.get("boundary"))
    shown = ", ".join(h for h in header if h)
    column = key.get("column")
    if column is None:
        column = next((c for g in _KEY_GUESSES[boundary] if (c := _find_column(header, g))),
                      None)
        if column is None:
            raise DatasetError(f"name the key column: key={{'boundary': ..., 'column': ...}}. "
                               f"The file has: {shown}")
    elif not isinstance(column, str) or column not in header:
        raise DatasetError(f"key column {column!r} is not in the file. The file has: {shown}")
    spec: dict[str, Any] = {"boundary": boundary, "column": column}
    for field_name, needed, default in (("year_column", "yearly", "year"),
                                        ("date_column", "daily", "date")):
        given = key.get(field_name)
        if temporal != needed:
            if given is not None:
                raise DatasetError(f"{field_name} is only used by a {needed} table")
            continue
        if given is None:
            given = _find_column(header, default)
            if given is None:
                raise DatasetError(f"a {needed} table needs a {default} column; name it with "
                                   f"key={{..., {field_name!r}: <column>}}. The file has: {shown}")
        elif not isinstance(given, str) or given not in header:
            raise DatasetError(f"{field_name} {given!r} is not in the file. The file has: {shown}")
        if given == column:
            raise DatasetError(f"the key column and the {field_name} are the same column")
        spec[field_name] = given
    return spec


def _missing_sql(expr: str) -> str:
    tokens = ", ".join(_lit(t) for t in _MISSING_TOKENS)
    return f"({expr} = '' OR upper({expr}) IN ({tokens}))"


def _date_sql(expr: str) -> str:
    fmts = "[" + ", ".join(_lit(f) for f in _DATE_FORMATS) + "]"
    return f"CAST(try_strptime({expr}, {fmts}) AS DATE)"


def _column_counts(con: Any, columns: Sequence[str]) -> dict[str, tuple[int, int, int]]:
    """Per column: (rows, missing, unparseable) over the non-blank rows of ``clean``."""
    if not columns:
        return {}
    parts = []
    for i, col in enumerate(columns):
        v = f"coalesce(trim({_q(col)}), '')"
        parts.append(f"count(*) FILTER (WHERE {_missing_sql(v)}) AS m{i}")
        parts.append(f"count(*) FILTER (WHERE NOT {_missing_sql(v)} "
                     f"AND TRY_CAST({v} AS DOUBLE) IS NULL) AS f{i}")
    row = con.execute(f"SELECT count(*), {', '.join(parts)} FROM clean").fetchone()
    n = int(row[0])
    return {col: (n, int(row[1 + 2 * i]), int(row[2 + 2 * i])) for i, col in enumerate(columns)}


def _variables(variables: Any, header: Sequence[str], taken: Sequence[str],
               con: Any) -> list[dict]:
    """The linked columns: as given (``name``, optional ``column``), or every numeric one."""
    given = normalise_variables(variables, extra=("column",))
    shown = ", ".join(h for h in header if h)
    if given is None:
        candidates = [h for h in header if h and h not in taken]
        counts = _column_counts(con, candidates)
        chosen = []
        for col in candidates:
            n, missing, failed = counts[col]
            present = n - missing
            if present and failed / max(1, n) <= _NON_NUMERIC_TOLERANCE and failed < present:
                chosen.append(col)
        if not chosen:
            raise DatasetError(f"the file has no numeric value column besides the key and time "
                               f"columns. The file has: {shown}")
        out, used = [], set()
        for col in chosen:
            name = col if _plain(col) else slug(col)
            base, k = name, 2
            while name.lower() in used:
                name, k = f"{base}_{k}", k + 1
            used.add(name.lower())
            out.append({"name": name, "unit": None, "description": None, "column": col})
        return out
    for var in given:
        col = var.get("column", var["name"])
        if not isinstance(col, str) or col not in header:
            raise DatasetError(f"value column {col!r} is not in the file. The file has: {shown}")
        if col in taken:
            raise DatasetError(f"value column {col!r} is the key or time column")
        var["column"] = col
    cols = [v["column"] for v in given]
    if len(set(cols)) != len(cols):
        raise DatasetError("a file column is linked twice")
    return given


def _plain(col: str) -> bool:
    from spacescans.custom.prepare import RESERVED_NAMES, _IDENTIFIER
    return bool(_IDENTIFIER.fullmatch(col)) and len(col) <= 60 and col.lower() not in RESERVED_NAMES


def prepare_table(inputs: Sequence[Input], work: Path, *, name: str, temporal: str, key: Any,
                  variables: Any) -> Prepared:
    """Check a polygon table and write ``values.parquet`` into ``work``."""
    import duckdb

    if len(inputs) != 1:
        raise DatasetError("a polygon table is registered from exactly one file")
    source = inputs[0]
    if source.period is not None:
        raise DatasetError("a polygon table takes its periods from a year or date column, "
                           "not from the file list")
    spill = work / ".duckdb_tmp"
    con = duckdb.connect(config={"temp_directory": str(spill)})
    try:
        header = _open_table(con, source.path)
        spec = _key_spec(key, header, temporal)
        time_col = spec.get("year_column") or spec.get("date_column")
        taken = [spec["column"]] + ([time_col] if time_col else [])
        all_blank = " AND ".join(f"coalesce(trim({_q(c)}), '') = ''" for c in header) or "false"
        con.execute(f"CREATE VIEW clean AS SELECT * FROM raw WHERE NOT ({all_blank})")
        if not con.execute("SELECT count(*) FROM clean").fetchone()[0]:
            raise DatasetError(f"{source.name} has a header but no data rows")
        vars_ = _variables(variables, header, taken, con)
        details = _check(con, spec, vars_, temporal, source.path.name)
        out = work / VALUES_FILE
        _write(con, spec, vars_, temporal, out)
        details.update(_facts(con, out, vars_, temporal))
    finally:
        con.close()
        shutil.rmtree(spill, ignore_errors=True)
    sha, size = sha256_of(source.path)
    prepared_sha, _ = sha256_of(out)
    boundary = spec["boundary"]
    span = details.get("coverage_dates") or details.get("coverage_years")
    when = f", {span[0]} to {span[1]}" if temporal != "static" and span else ""
    details.update({"boundary": boundary, "columns": {v["name"]: v["column"] for v in vars_},
                    "prepared_sha256": {VALUES_FILE: prepared_sha}})
    return Prepared(
        key=spec, variables=public_variables(vars_), files=[VALUES_FILE], details=details,
        originals=[{"name": source.name, "sha256": sha, "bytes": size, "path": source.path}],
        description=f"Registered {temporal} {BOUNDARY_SPEC[boundary]['label']} table{when}.")


def _check(con: Any, spec: Mapping[str, Any], vars_: Sequence[Mapping[str, Any]],
           temporal: str, filename: str) -> dict[str, Any]:
    """The upload path's rules, as counts; raises :class:`DatasetError`."""
    boundary = spec["boundary"]
    width = int(BOUNDARY_SPEC[boundary]["key_len"])
    k = f"coalesce(trim({_q(spec['column'])}), '')"
    parts = [
        "count(*)",
        f"count(*) FILTER (WHERE {k} = '')",
        f"count(*) FILTER (WHERE {k} <> '' AND NOT regexp_full_match({k}, '[0-9]{{{width}}}'))",
        f"count(*) FILTER (WHERE regexp_full_match({k}, '[0-9]{{{width - 1}}}'))",
    ]
    t = None
    if temporal == "yearly":
        t = f"coalesce(trim({_q(spec['year_column'])}), '')"
        y = f"TRY_CAST({t} AS DOUBLE)"
        parts.append(f"count(*) FILTER (WHERE {y} IS NULL OR {y} <> floor({y}) "
                     f"OR {y} < 1900 OR {y} > 2100)")
    elif temporal == "daily":
        t = f"coalesce(trim({_q(spec['date_column'])}), '')"
        d = _date_sql(t)
        parts.append(f"count(*) FILTER (WHERE {d} IS NULL OR year({d}) < 1900 "
                     f"OR year({d}) > 2100)")
    row = con.execute(f"SELECT {', '.join(parts)} FROM clean").fetchone()
    rows, blank, bad, short = (int(x) for x in row[:4])
    if rows == 0:
        raise DatasetError(f"{filename} has a header but no data rows")
    col = spec["column"]
    if blank:
        raise DatasetError(f"{blank:,} of {rows:,} rows have no value in {col!r}")
    if bad:
        hint = ""
        if short:
            hint = (f" {short:,} of them {'is' if short == 1 else 'are'} one digit short, which "
                    "usually means a spreadsheet dropped a leading zero; "
                    f"{boundary} codes are zero-padded to {width} digits.")
        raise DatasetError(f"{bad:,} of {rows:,} values in {col!r} are not {width}-digit "
                           f"{boundary} codes.{hint}")
    if t is not None and int(row[4]):
        what = ("a whole-number year in 1900-2100" if temporal == "yearly" else
                "a date (YYYY-MM-DD, YYYY/MM/DD, YYYYMMDD or MM/DD/YYYY) in 1900-2100")
        tcol = spec.get("year_column") or spec.get("date_column")
        raise DatasetError(f"{int(row[4]):,} of {rows:,} rows have no usable value in {tcol!r} "
                           f"(expected {what})")
    group = [k]
    if temporal == "yearly":
        group.append(f"CAST(TRY_CAST({t} AS DOUBLE) AS INTEGER)")
    elif temporal == "daily":
        group.append(_date_sql(t))
    dupes = con.execute(
        f"SELECT coalesce(sum(c - 1), 0) FROM (SELECT count(*) AS c FROM clean "
        f"GROUP BY {', '.join(group)} HAVING count(*) > 1)").fetchone()[0]
    if int(dupes):
        what = {"static": "geography", "yearly": "geography and year",
                "daily": "geography and date"}[temporal]
        raise DatasetError(f"{int(dupes):,} duplicate rows: each {what} must appear once")
    counts = _column_counts(con, [v["column"] for v in vars_])
    blank_cols = [v["column"] for v in vars_ if counts[v["column"]][1] == rows]
    if blank_cols:
        raise DatasetError(f"value column(s) with no data at all: {', '.join(blank_cols)}")
    mostly = [(v["column"], counts[v["column"]][2]) for v in vars_
              if counts[v["column"]][2] / max(1, rows) > _NON_NUMERIC_TOLERANCE]
    if mostly:
        detail = ", ".join(f"{c} ({n:,} rows)" for c, n in mostly)
        raise DatasetError(f"value column(s) mostly non-numeric: {detail}. Exposure values "
                           "must be numbers.")
    return {"rows_in_file": rows,
            "unparsed": {v["name"]: counts[v["column"]][2] for v in vars_
                         if counts[v["column"]][2]}}


def _write(con: Any, spec: Mapping[str, Any], vars_: Sequence[Mapping[str, Any]],
           temporal: str, out: Path) -> None:
    selects = [f"coalesce(trim({_q(spec['column'])}), '') AS {KEY_COLUMN}"]
    order = [KEY_COLUMN]
    if temporal == "yearly":
        t = f"coalesce(trim({_q(spec['year_column'])}), '')"
        selects.append(f"CAST(TRY_CAST({t} AS DOUBLE) AS INTEGER) AS year")
        order.append("year")
    elif temporal == "daily":
        t = f"coalesce(trim({_q(spec['date_column'])}), '')"
        selects.append(f"{_date_sql(t)} AS date")
        order.append("date")
    for var in vars_:
        v = f"coalesce(trim({_q(var['column'])}), '')"
        num = f"TRY_CAST({v} AS DOUBLE)"
        selects.append(f"CASE WHEN {_missing_sql(v)} OR NOT isfinite({num}) THEN NULL "
                       f"ELSE {num} END AS {_q(var['name'])}")
    con.execute(f"COPY (SELECT {', '.join(selects)} FROM clean ORDER BY {', '.join(order)}) "
                f"TO {_lit(str(out))} (FORMAT PARQUET, COMPRESSION ZSTD)")


def _facts(con: Any, out: Path, vars_: Sequence[Mapping[str, Any]],
           temporal: str) -> dict[str, Any]:
    """Row and key counts and the time span of the written table."""
    cols = ["count(*)", f"count(DISTINCT {KEY_COLUMN})"]
    if temporal == "yearly":
        cols += ["min(year)", "max(year)"]
    elif temporal == "daily":
        cols += ["min(date)", "max(date)", "count(DISTINCT date)"]
    cols += [f"count({_q(v['name'])})" for v in vars_]
    row = con.execute(f"SELECT {', '.join(cols)} FROM read_parquet({_lit(str(out))})").fetchone()
    facts: dict[str, Any] = {"row_count": int(row[0]), "distinct_keys": int(row[1])}
    i = 2
    if temporal == "yearly":
        facts["coverage_years"] = [int(row[2]), int(row[3])]
        i = 4
    elif temporal == "daily":
        facts["coverage_dates"] = [row[2].isoformat(), row[3].isoformat()]
        facts["coverage_years"] = [row[2].year, row[3].year]
        facts["distinct_dates"] = int(row[4])
        i = 5
    facts["values_present"] = {v["name"]: int(row[i + j]) for j, v in enumerate(vars_)}
    return facts


def table_definition(record: DatasetRecord) -> dict[str, Any]:
    """The linkable definition of a registered polygon table."""
    from spacescans.custom.definition import STATIC_COVERAGE_YEARS

    details = record.details
    boundary = (record.key or {}).get("boundary") or details.get("boundary")
    if boundary not in BOUNDARY_SPEC:
        raise DatasetError(f"dataset {record.name!r} names no known boundary")
    years = (details.get("coverage_years") if record.temporal != "static"
             else list(STATIC_COVERAGE_YEARS))
    d = common_definition(record, geometry="polygon", boundary=boundary,
                          spatial_method="areal", coverage_years=years)
    d.update({
        "join_col": BOUNDARY_SPEC[boundary]["join_col"],
        "key_col": KEY_COLUMN,
        "year_col": "year" if record.temporal == "yearly" else None,
        "date_col": "date" if record.temporal == "daily" else None,
        "values_path": os.fspath(record.path(VALUES_FILE)),
        "row_count": details.get("row_count"),
        "distinct_keys": details.get("distinct_keys"),
        "sha256": (details.get("prepared_sha256") or {}).get(VALUES_FILE),
    })
    if record.temporal == "daily":
        d["coverage_dates"] = list(details.get("coverage_dates") or [])
    return d
