test_that("requests are encoded the way the bridge expects", {
  json <- spacescans:::.ss_to_json(list(a = I("x"), b = NULL, c = NA, d = 270, e = 2.5, f = c(1L, 2L),
                                        g = spacescans:::.ss_named(list())))
  expect_equal(as.character(json), '{"a":["x"],"b":null,"c":null,"d":270,"e":2.5,"f":[1,2],"g":{}}')
})

test_that("bridge args must be named", {
  expect_error(spacescans:::.ss_named(list(1, 2)), class = "spacescans_error_internal")
  expect_equal(names(spacescans:::.ss_named(NULL)), character(0))
})

test_that("parsed JSON is simplified to vectors where it is safe", {
  x <- jsonlite::parse_json('{"a":["x","y"],"b":[1,2.5],"c":[{"k":1}],"d":null,"e":{"f":[true]}}')
  s <- spacescans:::.ss_simplify(x)
  expect_equal(s$a, c("x", "y"))
  expect_equal(s$b, c(1, 2.5))
  expect_equal(s$c, list(list(k = 1L)))
  expect_null(s$d)
  expect_true("d" %in% names(s))
  expect_equal(s$e$f, TRUE)
})
