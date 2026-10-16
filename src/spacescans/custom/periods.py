"""Which days each file of a registered dataset stands for.

A yearly, biweekly or daily raster dataset is a series of GeoTIFFs, one per
period; a yearly features dataset can be one file per year. Each file's
period comes, in this order of precedence, from

1. the ``{path: period}`` mapping given to ``register`` (a year, or a start
   date as ``date`` / ``"YYYY-MM-DD"``; a biweekly raster may give
   ``(start, end)``);
2. a manifest: one ``.csv`` file passed with the others, with a ``file``
   column (the file's name) and ``year`` (yearly), ``date`` (daily) or
   ``start`` and optionally ``end`` (biweekly);
3. the file name: one year (``ndvi_2016.tif``) for yearly data; one date
   (``pm25_20160101.tif``, ``2016-01-01``, ``2016_01_01``, or the
   ``A2016001`` year-and-day-of-year form) for daily and biweekly data, or two
   increasing dates (start and end) for biweekly data.

A biweekly period without an end runs to the day before the next period's
start, at most 16 days; the last one ends on the 15th or at the month's end
when it starts on the 1st or 16th (semi-monthly products), else after 14
days. Periods must not overlap.
"""
from __future__ import annotations

import csv
import io
import re
from calendar import monthrange
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from spacescans.custom.registry import DatasetError

_YEAR = re.compile(r"(?<!\d)(19\d{2}|20\d{2}|2100)(?!\d)")
_DATE = re.compile(r"(?<!\d)(\d{4})([-_.]?)(\d{2})\2(\d{2})(?!\d)")
_DOY = re.compile(r"(?<![A-Za-z0-9])A(\d{4})(\d{3})(?!\d)")
#: The longest biweekly period accepted (semi-monthly halves have up to 16 days).
MAX_BIWEEKLY_DAYS = 16
#: The longest period a manifest may give a biweekly file.
MAX_MANIFEST_DAYS = 31
FIRST_YEAR, LAST_YEAR = 1900, 2100


@dataclass(frozen=True)
class Period:
    """One file's period: ``year`` (yearly) or ``start``/``end`` (inclusive dates)."""

    year: int | None = None
    start: date | None = None
    end: date | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.year is not None:
            return {"year": self.year}
        return {"start": self.start.isoformat() if self.start else None,
                "end": self.end.isoformat() if self.end else None}


def _in_range(d: date) -> bool:
    return FIRST_YEAR <= d.year <= LAST_YEAR


def parse_date(value: Any) -> date | None:
    """A date from a ``date``/``datetime`` or ``YYYY-MM-DD`` / ``YYYY/MM/DD`` / ``YYYYMMDD`` text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            d = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        return d if _in_range(d) else None
    return None


def parse_year(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        year = value
    elif isinstance(value, float) and value.is_integer():
        year = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        year = int(value.strip())
    else:
        return None
    return year if FIRST_YEAR <= year <= LAST_YEAR else None


def years_in_name(name: str) -> list[int]:
    """Distinct 4-digit years (1900-2100) in a file name, standing alone."""
    stem = Path(name).name
    return sorted({int(m.group(1)) for m in _YEAR.finditer(stem)})


def dates_in_name(name: str) -> list[date]:
    """Distinct dates in a file name, in order of appearance (8-digit forms, else A<year><doy>)."""
    stem = Path(name).name
    found: list[date] = []
    for m in _DATE.finditer(stem):
        try:
            d = date(int(m.group(1)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            continue
        if _in_range(d) and d not in found:
            found.append(d)
    if found:
        return found
    for m in _DOY.finditer(stem):
        year, doy = int(m.group(1)), int(m.group(2))
        if FIRST_YEAR <= year <= LAST_YEAR and 1 <= doy <= (366 if year % 4 == 0 and (
                year % 100 != 0 or year % 400 == 0) else 365):
            d = date(year, 1, 1) + timedelta(days=doy - 1)
            if d not in found:
                found.append(d)
    return found


def read_manifest(path: Path, temporal: str) -> dict[str, dict[str, Any]]:
    """``{file name: {year} | {start[, end]}}`` from a manifest CSV."""
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise DatasetError(f"{path.name}: the manifest must be a UTF-8 CSV file") from None
    rows = list(csv.reader(io.StringIO(text)))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise DatasetError(f"{path.name}: the manifest has no rows")
    header = [h.strip().lower() for h in rows[0]]
    names = {"file": ("file", "filename", "file_name", "path", "name"),
             "year": ("year",), "start": ("start", "start_date", "date"),
             "end": ("end", "end_date")}

    def col(kind: str) -> int | None:
        return next((header.index(n) for n in names[kind] if n in header), None)

    i_file = col("file")
    if i_file is None:
        raise DatasetError(f"{path.name}: the manifest needs a 'file' column")
    wanted = "year" if temporal == "yearly" else "start"
    i_main, i_end = col(wanted), col("end")
    if i_main is None:
        need = "'year'" if temporal == "yearly" else "'start' (or 'date')"
        raise DatasetError(f"{path.name}: the manifest needs a {need} column")
    out: dict[str, dict[str, Any]] = {}
    for line, row in enumerate(rows[1:], start=2):
        cells = row + [""] * (len(header) - len(row))
        fname = Path(cells[i_file].strip()).name
        if not fname:
            raise DatasetError(f"{path.name}: line {line} names no file")
        if fname in out:
            raise DatasetError(f"{path.name}: {fname} is listed twice")
        entry: dict[str, Any] = {}
        if temporal == "yearly":
            year = parse_year(cells[i_main])
            if year is None:
                raise DatasetError(f"{path.name}: line {line} has no year in 1900-2100")
            entry["year"] = year
        else:
            start = parse_date(cells[i_main])
            if start is None:
                raise DatasetError(f"{path.name}: line {line} has no date (YYYY-MM-DD) in "
                                   "1900-2100")
            entry["start"] = start
            if i_end is not None and cells[i_end].strip():
                if temporal != "biweekly":
                    raise DatasetError(f"{path.name}: an 'end' column is only used by biweekly "
                                       "data")
                end = parse_date(cells[i_end])
                if end is None:
                    raise DatasetError(f"{path.name}: line {line} has an unusable end date")
                entry["end"] = end
        out[fname] = entry
    return out


def _given(value: Any, temporal: str, name: str) -> dict[str, Any]:
    """A period from the ``{path: period}`` mapping."""
    if temporal == "yearly":
        year = parse_year(value)
        if year is None:
            raise DatasetError(f"{name}: the year given for it is not a year in 1900-2100")
        return {"year": year}
    if isinstance(value, (tuple, list)) and len(value) == 2:
        start, end = parse_date(value[0]), parse_date(value[1])
        if start is None or end is None:
            raise DatasetError(f"{name}: the period given for it is not two dates")
        if temporal == "daily":
            if start != end:
                raise DatasetError(f"{name}: a daily file covers one day, not a period")
            return {"start": start}
        return {"start": start, "end": end}
    start = parse_date(value)
    if start is None:
        raise DatasetError(f"{name}: the date given for it is not a date (YYYY-MM-DD) in "
                           "1900-2100")
    return {"start": start}


def _from_name(name: str, temporal: str) -> dict[str, Any]:
    if temporal == "yearly":
        years = years_in_name(name)
        if len(years) != 1:
            why = "has no year" if not years else "has more than one year"
            raise DatasetError(f"{name} {why} in its name; give each file's year in a manifest "
                               "CSV (file,year) or as {path: year}")
        return {"year": years[0]}
    dates = dates_in_name(name)
    if len(dates) == 1:
        return {"start": dates[0]}
    if temporal == "biweekly" and len(dates) == 2 and dates[0] < dates[1]:
        return {"start": dates[0], "end": dates[1]}
    why = "has no date" if not dates else "has more than one date"
    raise DatasetError(f"{name} {why} in its name; give each file's start date in a manifest "
                       "CSV (file,start[,end]) or as {path: date}")


def _last_biweekly_end(start: date) -> date:
    if start.day == 1:
        return date(start.year, start.month, 15)
    if start.day == 16:
        return date(start.year, start.month, monthrange(start.year, start.month)[1])
    return start + timedelta(days=13)


def assign_periods(files: Sequence[tuple[str, Any]], temporal: str,
                   manifest: Mapping[str, Mapping[str, Any]] | None = None) -> list[Period]:
    """The period of each ``(file name, given period or None)``, parallel to ``files``.

    Checks: every file has a period; years (yearly) or start dates distinct;
    biweekly periods 1-16 days (1-31 from a manifest) and not overlapping.
    """
    if manifest is not None:
        extra = sorted(set(manifest) - {n for n, _ in files})
        if extra:
            raise DatasetError(f"the manifest lists file(s) not given: {', '.join(extra)}")
    raw: list[dict[str, Any]] = []
    for name, given in files:
        if given is not None:
            raw.append(_given(given, temporal, name))
        elif manifest is not None:
            if name not in manifest:
                raise DatasetError(f"{name} is not in the manifest")
            raw.append(dict(manifest[name]))
        else:
            raw.append(_from_name(name, temporal))
    if temporal == "yearly":
        years = [r["year"] for r in raw]
        dupes = sorted({y for y in years if years.count(y) > 1})
        if dupes:
            raise DatasetError(f"two files carry the year {dupes[0]}")
        return [Period(year=y) for y in years]
    starts = [r["start"] for r in raw]
    dupes = sorted({s for s in starts if starts.count(s) > 1})
    if dupes:
        raise DatasetError(f"two files start on {dupes[0].isoformat()}")
    if temporal == "daily":
        return [Period(start=s, end=s) for s in starts]
    order = sorted(range(len(raw)), key=lambda i: starts[i])
    ends: dict[int, date] = {}
    for pos, i in enumerate(order):
        end = raw[i].get("end")
        limit = MAX_MANIFEST_DAYS if end is not None else MAX_BIWEEKLY_DAYS
        if end is None:
            if pos + 1 < len(order):
                nxt = starts[order[pos + 1]]
                end = min(nxt - timedelta(days=1), starts[i] + timedelta(days=MAX_BIWEEKLY_DAYS - 1))
            else:
                end = _last_biweekly_end(starts[i])
        days = (end - starts[i]).days + 1
        if not 1 <= days <= limit:
            raise DatasetError(f"{files[i][0]}: a biweekly period of {days} days (expected 1-"
                               f"{limit}); give start and end dates in a manifest")
        ends[i] = end
    for a, b in pairwise(order):
        if ends[a] >= starts[b]:
            raise DatasetError(f"the periods of {files[a][0]} and {files[b][0]} overlap")
    return [Period(start=starts[i], end=ends[i]) for i in range(len(raw))]


def split_manifest(paths: Sequence[Path], data_suffixes: Sequence[str]
                   ) -> tuple[list[Path], Path | None]:
    """The data files and the one manifest (a ``.csv``) among ``paths``."""
    data, manifests = [], []
    for p in paths:
        low = p.name.lower()
        if low.endswith(".csv"):
            manifests.append(p)
        elif low.endswith(tuple(data_suffixes)):
            data.append(p)
        else:
            raise DatasetError(f"{p.name}: expected {' / '.join(data_suffixes)} files and at "
                               "most one manifest .csv")
    if len(manifests) > 1:
        raise DatasetError("give at most one manifest .csv")
    return data, (manifests[0] if manifests else None)
