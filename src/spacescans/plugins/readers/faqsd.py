"""FAQSD daily air quality reader (TRACT-level).

Reads FAQSD ozone and PM2.5 text files, joins with tract-level area weights and
the patient episode table, and produces a patient-level exposure DataFrame via
the same SQL logic as the v1 modularized script
(``C4_Linkage_TRACT_FAQSD.py``).

Because FAQSD requires three inputs (text files + weights + patients) and
produces patient-level output directly, this reader is *self-contained*: the
``faqsd`` linkage pattern calls ``compute_patient_exposure()`` and writes the
result.

Input text file schema (columns 0, 1, 4):
    Date (YYYY/MM/DD or Mon-DD-YYYY), FIPS (int), value (float)

Weights pkl schema:
    geoid (int32), GEOID10 (int64), value (float64)

Output schema:
    PATID (str), o3 (float64), pm25 (float64)
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pandas as pd

from spacescans.pipeline.registry import register_reader

# ---------------------------------------------------------------------------
# File discovery helpers
# ---------------------------------------------------------------------------

_WEIGHTS_CANDIDATES = [
    "output/python/270m/TRACT/C3/buffer270mTRACT25m.pkl",
    "output/Python/270m/TRACT/C3/buffer270mTRACT25m.pkl",
    "output_modularized/270m/TRACT/C3/buffer270mTRACT25m.pkl",
]

_O3_FILENAMES = [
    "2013_ozone_daily_8hour_maximum.txt",
    "2014_ozone_daily_8hour_maximum.txt",
    "2015_ozone_daily_8hour_maximum.txt",
    "2016_ozone_daily_8hour_maximum.txt",
    "2017_ozone_daily_8hour_maximum.txt",
    "2018_ozone_daily_8hour_maximum.txt",
    "2019_ozone_daily_8hour_maximum.txt",
]

_PM25_FILENAMES = [
    "2013_pm25_daily_average.txt",
    "2014_pm25_daily_average.txt",
    "2015_pm25_daily_average.txt",
    "2016_pm25_daily_average.txt",
    "2017_pm25_daily_average.txt",
    "2018_pm25_daily_average.txt",
    "2019_pm25_daily_average.txt",
]


def _find_weights(repo_root: Path) -> Path:
    for rel in _WEIGHTS_CANDIDATES:
        p = repo_root / rel
        if p.exists():
            return p
    raise FileNotFoundError(
        "TRACT weight file not found. Tried:\n"
        + "\n".join(f"  {repo_root / r}" for r in _WEIGHTS_CANDIDATES)
    )


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_faqsd_dates(series: pd.Series) -> pd.Series:
    """Parse FAQSD date column, handling both YYYY/MM/DD and Mon-DD-YYYY formats."""
    return pd.to_datetime(series, format="mixed", dayfirst=False).dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _read_o3(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(str(path), usecols=[0, 1, 4])
    df.columns = ["date", "fips", "o3"]
    df["date"] = _parse_faqsd_dates(df["date"])
    df["fips"] = pd.to_numeric(df["fips"], errors="coerce")
    df["o3"] = pd.to_numeric(df["o3"], errors="coerce")
    return df.dropna(subset=["fips"])


def _read_pm25(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(str(path), usecols=[0, 1, 4])
    df.columns = ["date", "fips", "pm25"]
    df["date"] = _parse_faqsd_dates(df["date"])
    df["fips"] = pd.to_numeric(df["fips"], errors="coerce")
    df["pm25"] = pd.to_numeric(df["pm25"], errors="coerce")
    return df.dropna(subset=["fips"])


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

@register_reader("faqsd")
class FAQSDExposureSource:
    """Self-contained FAQSD reader.

    ``compute_patient_exposure()`` is the main entry point called by the
    ``faqsd_daily_areal`` linkage pattern.  It returns a patient-level
    DataFrame with columns [PATID, o3, pm25].
    """

    def __init__(self, config):
        self.config = config

    # ------------------------------------------------------------------
    # load_exposure() returns tract-level daily table (date × fips × vars)
    # This is used if someone calls the reader in non-self-contained mode.
    # ------------------------------------------------------------------

    def load_exposure(self, *, years=None) -> pd.DataFrame:
        data_dir = Path(self.config.exposure.file)
        if not data_dir.is_dir():
            data_dir = data_dir.parent

        repo_root = Path(os.getcwd())
        weights = self._load_weights(repo_root)
        tract_list = set(weights["GEOID10"].unique().tolist())

        o3_files = [data_dir / fn for fn in _O3_FILENAMES if (data_dir / fn).exists()]
        pm25_files = [data_dir / fn for fn in _PM25_FILENAMES if (data_dir / fn).exists()]

        o3 = pd.concat([_read_o3(f).loc[lambda d: d["fips"].isin(tract_list)] for f in o3_files],
                       ignore_index=True)
        pm25 = pd.concat([_read_pm25(f).loc[lambda d: d["fips"].isin(tract_list)] for f in pm25_files],
                         ignore_index=True)

        con = sqlite3.connect(":memory:")
        o3.to_sql("o3", con, index=False, if_exists="replace")
        pm25.to_sql("pm25", con, index=False, if_exists="replace")
        faqsd = pd.read_sql(
            """
            SELECT o3.date, o3.fips,
                   o3.o3   AS o3,
                   pm.pm25 AS pm25
            FROM o3
            LEFT JOIN pm25 AS pm ON o3.date = pm.date AND o3.fips = pm.fips
            """,
            con,
        )
        con.close()
        return faqsd

    # ------------------------------------------------------------------
    # compute_patient_exposure() — full pipeline, returns patient-level df
    # ------------------------------------------------------------------

    def compute_patient_exposure(self, patients: pd.DataFrame) -> pd.DataFrame:
        """Full FAQSD linkage: text → tract weights → patient TWA.

        Replicates the exact SQL in C4_Linkage_TRACT_FAQSD.py.

        Parameters
        ----------
        patients:
            Patient episode table from the RDS file.  Must contain
            [PATID, geoid, start, end].

        Returns
        -------
        pd.DataFrame
            Columns: [PATID (str), o3 (float64), pm25 (float64)]
        """
        repo_root = Path(os.getcwd())
        weights = self._load_weights(repo_root)
        tract_list = set(weights["GEOID10"].unique().tolist())

        data_dir = Path(self.config.exposure.file)
        if not data_dir.is_dir():
            data_dir = data_dir.parent

        o3_files = [data_dir / fn for fn in _O3_FILENAMES if (data_dir / fn).exists()]
        pm25_files = [data_dir / fn for fn in _PM25_FILENAMES if (data_dir / fn).exists()]

        if not o3_files:
            raise FileNotFoundError(f"No FAQSD ozone text files found in {data_dir}")
        if not pm25_files:
            raise FileNotFoundError(f"No FAQSD PM2.5 text files found in {data_dir}")

        o3 = pd.concat([_read_o3(f).loc[lambda d: d["fips"].isin(tract_list)] for f in o3_files],
                       ignore_index=True)
        pm25 = pd.concat([_read_pm25(f).loc[lambda d: d["fips"].isin(tract_list)] for f in pm25_files],
                         ignore_index=True)

        # Prepare patients
        vsehr = patients[["PATID", "geoid", "start", "end"]].copy()
        vsehr["PATID"] = vsehr["PATID"].astype(str)
        vsehr["geoid"] = pd.to_numeric(vsehr["geoid"], errors="coerce")
        vsehr["start"] = pd.to_datetime(vsehr["start"]).dt.strftime("%Y-%m-%d")
        vsehr["end"] = pd.to_datetime(vsehr["end"]).dt.strftime("%Y-%m-%d")
        vsehr = vsehr.dropna(subset=["geoid"])

        con = sqlite3.connect(":memory:")
        try:
            o3.to_sql("o3", con, index=False, if_exists="replace")
            pm25.to_sql("pm25", con, index=False, if_exists="replace")

            # Merge o3 and pm25 on date × fips
            pd.read_sql(
                """
                SELECT o3.date, o3.fips,
                       o3.o3   AS o3,
                       pm.pm25 AS pm25
                FROM o3
                LEFT JOIN pm25 AS pm ON o3.date = pm.date AND o3.fips = pm.fips
                """,
                con,
            ).to_sql("faqsd", con, index=False, if_exists="replace")

            vsehr.to_sql("vsehr_rh", con, index=False, if_exists="replace")
            weights.to_sql("buffer270mTRACT25m", con, index=False, if_exists="replace")

            # Patient × tract mapping with area weights
            pd.read_sql(
                """
                SELECT
                    v.PATID, v.start, v.end,
                    b.GEOID10 AS fips,
                    b.value   AS aw
                FROM vsehr_rh AS v
                LEFT OUTER JOIN buffer270mTRACT25m AS b
                  ON v.geoid = b.geoid
                """,
                con,
            ).to_sql("dat_map", con, index=False, if_exists="replace")

            # Area-weighted daily values per patient × date
            pd.read_sql(
                """
                SELECT
                    d.PATID,
                    f.date,
                    SUM(d.aw * f.o3)
                        / NULLIF(SUM(CASE WHEN f.o3   IS NOT NULL THEN d.aw ELSE 0 END), 0) AS o3_aw,
                    SUM(d.aw * f.pm25)
                        / NULLIF(SUM(CASE WHEN f.pm25 IS NOT NULL THEN d.aw ELSE 0 END), 0) AS pm25_aw
                FROM dat_map AS d
                JOIN faqsd AS f
                  ON d.fips = f.fips
                 AND date(f.date) BETWEEN date(d.start) AND date(d.end)
                GROUP BY d.PATID, f.date
                """,
                con,
            ).to_sql("daily_aw", con, index=False, if_exists="replace")

            # Simple average across all days per patient
            result = pd.read_sql(
                """
                SELECT PATID,
                       AVG(o3_aw)   AS o3,
                       AVG(pm25_aw) AS pm25
                FROM daily_aw
                GROUP BY PATID
                """,
                con,
            )
        finally:
            con.close()

        return result[["PATID", "o3", "pm25"]]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_weights(self, repo_root: Path) -> pd.DataFrame:
        # Prefer configured source file, fall back to candidates
        src = Path(self.config.source.file)
        if src.exists():
            return pd.read_pickle(str(src)) if src.suffix == ".pkl" else pd.read_csv(str(src))
        return pd.read_pickle(str(_find_weights(repo_root)))


def _find_weights(repo_root: Path) -> Path:
    for rel in _WEIGHTS_CANDIDATES:
        p = repo_root / rel
        if p.exists():
            return p
    raise FileNotFoundError(
        "TRACT weight file not found. Tried:\n"
        + "\n".join(f"  {repo_root / r}" for r in _WEIGHTS_CANDIDATES)
    )
