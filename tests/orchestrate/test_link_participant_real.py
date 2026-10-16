"""The participant level on real catalog data equals the episode level weighted by days (opt-in).

``orchestrate.link`` runs the golden synth_1k cohort twice against the real data root, at
``output_level="episode"`` and at ``"participant"`` (one weight cache, so the C3 steps run
once), for a static areal source (noise) and the yearly line distances of TIGER. The
participant table must have one row per participant in order of first appearance and, to
1e-9, the two-stage form of the participant formula built from the episode table:

    x_iv = sum_e D_ev * x_ev / sum_e D_ev

with D_ev the episode's contributing days: its length when it has a value (noise), and its
days in each year whose road distance is defined in the run's own C3 output (TIGER).
Linkage rates count participants. No cohort value is printed.

Opt-in: ``SPACESCANS_REAL_DATA_DIR`` (the data root) and ``SPACESCANS_GOLDEN_DIR`` (for the
synth_1k cohort), read at import. Takes a few minutes; run it under the host's heavy lock.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

_DATA_ENV = os.environ.get("SPACESCANS_REAL_DATA_DIR")
_GOLDEN_ENV = os.environ.get("SPACESCANS_GOLDEN_DIR")
DATA = Path(_DATA_ENV) if _DATA_ENV else None
COHORT = Path(_GOLDEN_ENV) / "inputs" / "synth_1k.csv" if _GOLDEN_ENV else None

pytestmark = [
    pytest.mark.slow,
    pytest.mark.geo,
    pytest.mark.extras,
    pytest.mark.skipif(DATA is None or not DATA.is_dir() or COHORT is None
                       or not COHORT.is_file(),
                       reason="opt-in: set SPACESCANS_REAL_DATA_DIR and SPACESCANS_GOLDEN_DIR"),
]


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    from spacescans.orchestrate import WeightCache, link

    out = tmp_path_factory.mktemp("participant-real")
    cache = WeightCache(out / "cache", key_format="v2")
    res = {}
    for level in ("episode", "participant"):
        res[level] = link(COHORT, ["noise", "tiger_proximity"], data_dir=DATA,
                          run_dir=out / level, cache=cache, output_level=level)
        assert res[level].ok, res[level].errors
    return res


def _episodes(run_dir: Path):
    import pandas as pd

    t = pd.read_parquet(run_dir / "input.parquet")
    return pd.DataFrame({"pid": t["pid"].astype(str).to_numpy(),
                         "episode_id": t["episode_id"].astype(int).to_numpy(),
                         "start": pd.to_datetime(t["startDate"]), "end": pd.to_datetime(t["endDate"])})


def _days(ep, lo, hi):
    s = ep["start"].where(ep["start"] > lo, lo)
    e = ep["end"].where(ep["end"] < hi, hi)
    d = (e - s).dt.days + 1
    return np.where(ep["start"].notna() & ep["end"].notna() & (d > 0), d.fillna(0), 0.0)


def _two_stage(ep, x, d):
    import pandas as pd

    x = pd.Series(np.asarray(x, float), index=ep["episode_id"])
    d = pd.Series(np.asarray(d, float), index=ep["episode_id"])
    assert ((x.notna()) == (d > 0)).all(), "a value without contributing days, or the reverse"
    pid = pd.Series(ep["pid"].to_numpy(), index=ep["episode_id"])
    num = (x.fillna(0) * d).groupby(pid).sum()
    den = d.groupby(pid).sum()
    return num / den.where(den > 0)


def _check(participant, ref, col, order):
    have = participant.set_index("pid")[col].reindex(order).to_numpy(float)
    want = ref.reindex(order).to_numpy(float)
    assert (np.isnan(have) == np.isnan(want)).all(), f"{col}: missing participants differ"
    ok = np.isnan(want) | (np.abs(have - want) <= 1e-9)
    assert ok.all(), f"{col}: max abs diff {np.nanmax(np.abs(have - want)):.3g}"


def test_the_participant_table_has_one_row_per_participant(runs):
    ep = _episodes(runs["episode"].run_dir)
    part = runs["participant"].exposures
    assert list(part["pid"]) == list(dict.fromkeys(ep["pid"]))
    assert runs["participant"].output_level == "participant"
    assert list(part.columns[:1]) == ["pid"] and "episode_id" not in part.columns
    cov = runs["participant"].coverage
    assert (cov["participants"] == part["pid"].nunique()).all()


def test_noise_participant_means_weight_episodes_by_their_length(runs):
    ep = _episodes(runs["episode"].run_dir)
    epi = runs["episode"].exposures.set_index("episode_id").reindex(ep["episode_id"])
    order = list(dict.fromkeys(ep["pid"]))
    length = _days(ep, ep["start"].min(), ep["end"].max())
    for col in ("l50dba_exi", "l50dba_imp", "l50dba_nat"):
        x = epi[col].to_numpy(float)
        ref = _two_stage(ep, x, np.where(~np.isnan(x), length, 0.0))
        _check(runs["participant"].exposures, ref, col, order)


def test_tiger_participant_means_weight_each_year_with_a_distance(runs):
    import pandas as pd

    run_dir = runs["episode"].run_dir
    ep = _episodes(run_dir)
    annual = pd.read_parquet(run_dir / "output" / "c3_tiger_roads.parquet")
    epi = runs["episode"].exposures.set_index("episode_id").reindex(ep["episode_id"])
    order = list(dict.fromkeys(ep["pid"]))
    years = sorted(int(y) for y in annual["year"].unique())
    for col in ("dist_pri", "dist_sec", "dist_prisec"):
        d = np.zeros(len(ep))
        for y in years:
            have = set(annual.loc[(annual["year"] == y) & annual[col].notna(), "geoid"].astype(int))
            in_year = _days(ep, pd.Timestamp(f"{y}-01-01"), pd.Timestamp(f"{y}-12-31"))
            d += np.where(ep["episode_id"].isin(have), in_year, 0.0)
        ref = _two_stage(ep, epi[col].to_numpy(float), d)
        _check(runs["participant"].exposures, ref, col, order)
