wait_for <- function(cond, timeout = 15) {
  t0 <- Sys.time()
  while (!cond() && as.numeric(difftime(Sys.time(), t0, units = "secs")) < timeout) Sys.sleep(0.1)
  cond()
}

test_that("wait = FALSE returns a job whose result is the linked cohort", {
  dir <- local_fake_bridge(list(link = list(sleep_s = 1)))
  co <- fake_cohort()
  job <- ss_link(co, "ndi", data_dir = tempdir(), cache = FALSE, wait = FALSE)
  expect_s3_class(job, "ss_job")
  st <- ss_job_status(job)
  expect_equal(st$state, "running")
  expect_output(print(st), "running")
  expect_error(spacescans:::.ss_job_finish(job), "still running")
  res <- ss_job_result(job, progress = FALSE)
  expect_equal(names(res), c(names(co), "ndi"))
  expect_identical(ss_job_result(job), res)
  expect_equal(ss_job_status(job)$state, "succeeded")
  expect_false(dir.exists(job$run_dir))
})

test_that("ss_job_wait() returns after a timeout with the job still running", {
  dir <- local_fake_bridge(list(link = list(sleep_s = 30)))
  job <- ss_link(fake_cohort(), "ndi", data_dir = tempdir(), cache = FALSE, wait = FALSE)
  st <- ss_job_wait(job, timeout = 0.5, progress = FALSE)
  expect_equal(st$state, "running")
  expect_true(wait_for(function() !is.na(ss_job_status(job)$frac)))
  expect_gt(ss_job_status(job)$events, 1L)
  st <- ss_job_cancel(job, grace = 5)
  expect_equal(st$state, "interrupted")
  expect_false(process_alive(job$pid))
  e <- tryCatch(ss_job_result(job), error = identity)
  expect_s3_class(e, "spacescans_error_interrupted")
  unlink(job$run_dir, recursive = TRUE)
})

test_that("a job with an explicit run_dir can be attached again, with or without the cohort", {
  dir <- local_fake_bridge(list(link = list(sleep_s = 0.5)))
  co <- fake_cohort()
  rd <- file.path(withr::local_tempdir(), "job1")
  job <- ss_link(co, c("ndi", "walkability"), data_dir = tempdir(), cache = FALSE, wait = FALSE,
                 run_dir = rd, keep = TRUE)
  expect_true(wait_for(function() ss_job_status(job)$state != "running"))
  again <- ss_job_attach(rd, cohort = co)
  expect_equal(ss_job_status(again)$state, "succeeded")
  res <- ss_job_result(again)
  expect_equal(names(res), c(names(co), "ndi", "NatWalkInd"))
  bare <- ss_job_result(ss_job_attach(rd))
  expect_equal(bare$episode_id, 0:5)
  expect_equal(bare$ndi, res$ndi)
  other <- co
  other$longitude[1] <- other$longitude[1] + 1
  expect_error(ss_job_attach(rd, cohort = other), class = "spacescans_error_cohort")
  expect_error(ss_job_attach(withr::local_tempdir()), "not a spacescans job directory")
})

test_that("an attached running job can be cancelled through its process tree marker", {
  dir <- local_fake_bridge(list(link = list(sleep_s = 30)))
  rd <- file.path(withr::local_tempdir(), "job2")
  job <- ss_link(fake_cohort(), "ndi", data_dir = tempdir(), cache = FALSE, wait = FALSE, run_dir = rd)
  again <- ss_job_attach(rd)
  expect_equal(ss_job_status(again)$state, "running")
  expect_true(wait_for(function() !is.na(ss_job_status(again)$frac)))
  ss_job_cancel(again, grace = 5)
  expect_true(wait_for(function() !process_alive(job$pid), 5))
  expect_equal(ss_job_status(again)$state, "interrupted")
})

test_that("non-jobs are refused", {
  expect_error(ss_job_status(list()), class = "spacescans_error_config")
})
