#!/usr/bin/env bash
# Build the spacescans wheel from this repository and bundle it into the R
# package (r/inst/python/), with a pin.json that ties it to the commit.
#
#   r/tools/build-wheel.sh [--allow-dirty] [--url URL]
#
# How the wheel stays tied to the same commit:
#   * The wheel is built from `git archive HEAD` (only committed files), so it
#     is exactly the Python code of the commit the R package is built from.
#     A tree with uncommitted changes outside r/ is refused unless
#     --allow-dirty is given (then the working tree is used, pin.json records
#     git_dirty: true, and the build must not be released).
#   * The Python version (pyproject.toml), the R package Version and
#     Config/spacescans/version in r/DESCRIPTION must be identical, or the
#     script stops. R and Python are released with the same version and tag.
#   * The build sets SOURCE_DATE_EPOCH to the commit time, so building the same
#     commit again gives a byte-identical wheel (same sha256).
#   * pin.json records version, wheel file name, sha256, the code fingerprint
#     of the wheel's .py files (the value the bridge reports as code_fp once
#     installed), the commit and the build time. ss_install() refuses a
#     bundled wheel whose sha256 does not match, and the R package warns when
#     the running code_fp differs from the pinned one.
#   * Neither the wheel nor pin.json is committed on ordinary commits
#     (.gitignore). The release job runs this script at the release commit
#     with --url pointing at the wheel attached to the GitHub release, then
#     `R CMD build r`; the tarball carries wheel + pin.json. Installs from git
#     (remotes/R-universe) get pin.json from the release commit and download
#     the wheel from that URL, checking the sha256.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
r_dir="$(dirname "$here")"
repo="$(dirname "$r_dir")"
GIT="${GIT:-git}"
if [ "$(uname)" = "Darwin" ] && [ -x /Library/Developer/CommandLineTools/usr/bin/git ] && [ -z "${GIT_SET:-}" ]; then
  GIT=/Library/Developer/CommandLineTools/usr/bin/git   # avoids the Xcode licence prompt
fi
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

allow_dirty=0
url=""
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-dirty) allow_dirty=1 ;;
    --url) url="$2"; shift ;;
    *) echo "usage: $0 [--allow-dirty] [--url URL]" >&2; exit 2 ;;
  esac
  shift
done

cd "$repo"
py_version="$(sed -n 's/^version = "\(.*\)"$/\1/p' pyproject.toml | head -n 1)"
r_version="$(sed -n 's/^Version: *//p' r/DESCRIPTION)"
cfg_version="$(sed -n 's/^Config\/spacescans\/version: *//p' r/DESCRIPTION)"
if [ "$py_version" != "$r_version" ] || [ "$py_version" != "$cfg_version" ]; then
  echo "error: versions differ: pyproject $py_version, DESCRIPTION Version $r_version," \
       "Config/spacescans/version $cfg_version" >&2
  exit 1
fi

commit="$("$GIT" rev-parse HEAD)"
dirty="$("$GIT" status --porcelain --untracked-files=no -- . ':(exclude)r' | wc -l | tr -d ' ')"
if [ "$dirty" != "0" ] && [ "$allow_dirty" != "1" ]; then
  echo "error: uncommitted changes outside r/; commit them or pass --allow-dirty" >&2
  exit 1
fi

out="$r_dir/inst/python"
mkdir -p "$out"
rm -f "$out"/spacescans_pipeline-*.whl "$out/pin.json"
src="$(mktemp -d)"
trap 'rm -rf "$src"' EXIT
if [ "$dirty" = "0" ]; then
  "$GIT" archive --format=tar HEAD -- pyproject.toml README.md src | tar -x -C "$src"
  git_dirty=false
else
  cp -R pyproject.toml README.md src "$src/"
  find "$src" -name __pycache__ -type d -prune -exec rm -rf {} +
  git_dirty=true
fi
# Build into a scratch directory: uv also writes a .gitignore into --out-dir.
# SOURCE_DATE_EPOCH = commit time makes the wheel byte-identical for a commit.
SOURCE_DATE_EPOCH="$("$GIT" log -1 --format=%ct HEAD)" "$UV" build --wheel --out-dir "$src/dist" "$src" >&2
cp "$src"/dist/spacescans_pipeline-"$py_version"-*.whl "$out/"
wheel="$(ls "$out"/spacescans_pipeline-"$py_version"-*.whl)"
sha="$(shasum -a 256 "$wheel" | cut -d ' ' -f 1)"
# Same algorithm as spacescans._capabilities.code_fingerprint (stdlib only).
code_fp="$("$UV" run --no-project --python 3.12 python -I -c '
import sys; sys.path.insert(0, sys.argv[1])
from spacescans._capabilities import fingerprint_wheel
print(fingerprint_wheel(sys.argv[2]))' "$src/src" "$wheel")"
cat > "$out/pin.json" <<JSON
{
  "version": "$py_version",
  "wheel": "$(basename "$wheel")",
  "sha256": "$sha",
  "code_fp": "$code_fp",
  "git_commit": "$commit",
  "git_dirty": $git_dirty,
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "url": $( [ -n "$url" ] && printf '"%s"' "$url" || printf 'null' )
}
JSON
echo "bundled $(basename "$wheel") (sha256 $sha) from $commit into $out" >&2
