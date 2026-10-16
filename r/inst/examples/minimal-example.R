# A minimal spacescans example: three synthetic participants linked to the
# NPS noise model, the catalog source whose files are all on the project
# mirror. Before running it, once:
#   ss_install()              # the Python side
#   ss_data_dir("~/ss-data")  # where the exposure data goes
#   ss_fetch("noise")         # about 1.2 GB
# Then:
#   source(system.file("examples", "minimal-example.R", package = "spacescans"))
library(spacescans)

# Time-referenced geolocation data: one row per location episode. P2 moved
# once, so the three participants have four location episodes. The places
# are city centres, not people.
cohort <- data.frame(
  pid = c("P1", "P2", "P2", "P3"),
  startDate = as.Date(c("2015-01-01", "2014-03-01", "2016-07-01", "2017-01-01")),
  endDate = as.Date(c("2017-12-31", "2016-06-30", "2019-12-31", "2018-06-30")),
  longitude = c(-86.1581, -87.6298, -84.3880, -122.4194),
  latitude = c(39.7684, 41.8781, 33.7490, 37.7749)
)

if (!isTRUE(attr(ss_data_status("noise"), "ready"))) {
  stop('The noise data is not in ss_data_dir() yet: run ss_fetch("noise") first.')
}

# Participant level (the default): one row per participant, each value the
# mean over the participant's location episodes weighted by contributing days.
res <- ss_link(cohort, "noise")
print(res)
print(attr(res, "coverage"))      # linkage rates: the share of participants with a value
print(attr(res, "features"))      # what each column means

# Episode level: the cohort with the noise columns added, one row per episode.
episodes <- ss_link(cohort, "noise", output_level = "episode")
print(episodes)

# The two agree: noise is time-invariant, so every day of an episode
# contributes and P2's value is the day-weighted mean of its two episodes.
days <- as.numeric(episodes$endDate - episodes$startDate) + 1
p2 <- episodes$pid == "P2"
stopifnot(isTRUE(all.equal(res$l50dba_exi[res$pid == "P2"],
                           weighted.mean(episodes$l50dba_exi[p2], days[p2]))))
