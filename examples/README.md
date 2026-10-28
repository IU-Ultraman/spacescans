# Worked examples

`link_example.py` and `link_example.R` do the same thing from Python and from R:

1. define a synthetic cohort of three participants with five location episodes;
2. look up a catalog variable, download its data, and check the cohort's coverage;
3. link the cohort to NPS Soundscape Noise with one row per participant (the default) and with
   one row per location episode (the option);
4. register two user-supplied datasets once, a yearly table keyed by 2010 county and a set of
   point features (synthetic clinic sites), and link them by name with the catalog source.

After the example, each script recomputes its results independently: participant-level values
must equal the average of the participant's location-episode values weighted by contributing
days, the county values must equal day-weighted county values computed by hand, and the
distances must equal nearest distances computed separately in EPSG:5070. The exit status is 1
when a check fails.

```bash
pip install "spacescans[geo]"
export SPACESCANS_DATA_DIR=~/spacescans-data   # about 1.3 GB is downloaded here on the first run
python examples/link_example.py
```

```r
install.packages("spacescans",
                 repos = c("https://iu-ultraman.r-universe.dev", "https://cloud.r-project.org"))
spacescans::ss_install()
# then, from the root of this repository, with SPACESCANS_DATA_DIR set:
#   Rscript examples/link_example.R
```

The sections marked `# --- S2.n ... ---` (Python) and `# --- S3.n ... ---` (R) are the example
itself; the rest of each file runs it with a temporary dataset registry, so your own registry is
never touched, and checks the results. `outputs/` holds what the last run wrote: the linked
tables (`*_noise_*.csv`, `*_registered_*.csv`), what the example printed (`*_console.txt`, and
per section in `*_console_sections.json`), the checks with their largest differences
(`*_checks.csv`) and the run facts (`*_run.csv`). The cohort, the county table and the clinic
sites are synthetic.
