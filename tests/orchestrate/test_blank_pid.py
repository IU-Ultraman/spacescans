"""Rows with a blank participant id at the participant level (PHASE_D 7.1 item 1).

The web app's rule: a row whose ``pid`` is missing, empty or only spaces belongs to no
participant, so the participant table leaves it out (no pooled empty-id row) and the run
reports how many rows that was. ``cohort.validate`` already refuses a blank pid before
``link`` writes anything, so in ``link`` the rule is a second line of defence; the merge
helpers follow it for every caller (the web app reads run directories with them).
The ``link`` side is in test_link_unit.py. Synthetic ids only.
"""
from __future__ import annotations

import pandas as pd

from spacescans.orchestrate import merge


def _cohort_with_blanks() -> pd.DataFrame:
    return pd.DataFrame({
        "pid": ["A1", None, "B2", "", "A1", "   ", "C3"],
        "startDate": ["2014-01-01"] * 7,
        "endDate": ["2015-12-31"] * 7,
        "longitude": [-86.15, -86.10, -87.62, -84.39, -86.11, -85.0, -84.0],
        "latitude": [39.77, 39.80, 41.88, 33.75, 39.70, 40.0, 41.0],
    })


def test_participant_keys_leave_out_blank_ids_and_keep_first_appearance_order():
    cohort = _cohort_with_blanks()
    keys = merge.participant_keys(cohort)
    assert list(keys["pid"]) == ["A1", "B2", "C3"]
    assert merge.blank_pid_rows(cohort) == 3


def test_blank_ids_read_from_a_text_csv_or_a_numeric_column():
    as_text = pd.DataFrame({"PATID": ["7", pd.NA, " ", "8"]}, dtype="string")
    assert list(merge.participant_keys(as_text)["pid"]) == ["7", "8"]
    assert merge.blank_pid_rows(as_text) == 2
    numeric = pd.DataFrame({"pid": [7.0, float("nan"), 8.0]})
    assert merge.blank_pid_rows(numeric) == 1
    assert len(merge.participant_keys(numeric)) == 2


def test_no_blank_rows_counts_zero():
    cohort = pd.DataFrame({"pid": ["A1", "B2", "A1"]})
    assert merge.blank_pid_rows(cohort) == 0
    assert list(merge.participant_keys(cohort)["pid"]) == ["A1", "B2"]


def test_a_pooled_empty_id_row_of_a_c4_output_never_reaches_the_tables():
    """The C4 step groups the blank-id rows into one NULL-id row; the merge drops it."""
    cohort = _cohort_with_blanks()
    keys = merge.participant_keys(cohort)
    c4 = pd.DataFrame({"PATID": ["A1", None, "B2", "C3"], "v": [1.0, 99.0, 2.0, None]})
    table, stats = merge.merge_partial(keys, [("var", c4)], {"var": ["v"]},
                                       experiment="exp", level="participant")
    assert list(table["pid"]) == ["A1", "B2", "C3"]
    assert 99.0 not in set(table["v"].dropna())
    assert (stats.cohort_n, stats.matched_n) == (3, 2)
    fanned = merge.fan_in(cohort, [("exp", table)], level="participant")
    assert list(fanned["pid"]) == ["A1", "B2", "C3"]
    assert fanned["pid"].notna().all()
    assert list(fanned["v"].iloc[:2]) == [1.0, 2.0] and pd.isna(fanned["v"].iloc[2])


def test_episode_level_is_unchanged_by_the_rule():
    """Episode rows keep every input row, blank ids included (web 0.2.7)."""
    cohort = _cohort_with_blanks()
    keys = merge.episode_keys(cohort)
    assert list(keys["episode_id"]) == list(range(len(cohort)))
    fanned = merge.fan_in(cohort, [], level="episode")
    assert len(fanned) == len(cohort)
