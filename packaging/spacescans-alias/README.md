# spacescans (alias)

This distribution is an alias. The spacescans package is published on PyPI as
[`spacescans-pipeline`](https://pypi.org/project/spacescans-pipeline/), and that is the
name to install:

```bash
pip install "spacescans-pipeline[geo]"
```

`pip install spacescans` installs `spacescans-pipeline` at the same version and nothing else
(no extras: add them with the command above). Either way you `import spacescans`.

spacescans links time-referenced geolocation data (location episodes) to the spatial and
contextual exposome, per participant or per location episode. Documentation, source code and
the R package: <https://github.com/IU-Ultraman/spacescans>. Released under the MIT License.
