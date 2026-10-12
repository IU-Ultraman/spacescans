# Tests that need Python run only when SPACESCANS_PYTHON points at an
# environment with spacescans installed; the rest always run.
library(testthat)
library(spacescans)

test_check("spacescans")
