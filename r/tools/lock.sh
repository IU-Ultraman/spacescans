#!/usr/bin/env bash
# Write the hash-pinned dependency lock that ss_install() uses on macOS arm64:
#
#   r/inst/python/locks/macos-arm64-py312.txt          every dependency of every
#                                                      extra, with sha256 hashes
#   r/inst/python/locks/macos-arm64-py312.groups.json  which packages belong to
#                                                      the base install and to
#                                                      each extra
#
# The lock targets CPython 3.12 on Apple silicon with a macOS 14 floor
# (MACOSX_DEPLOYMENT_TARGET=14.0), so uv picks wheels that install on
# macOS 14 and later (rasterio 1.5.1 rather than 1.5.2, pyhdf 0.11.6 rather
# than 0.11.7). ss_install() installs the base packages plus those of the
# requested extras, with --require-hashes, then the spacescans wheel with
# --no-deps. Like the wheel, the lock is a release artefact (not committed on
# ordinary commits); regenerate it whenever pyproject.toml's dependencies
# change, and check it on macOS 14 and 15 runners.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(dirname "$(dirname "$here")")"
UV="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
out="$repo/r/inst/python/locks"
mkdir -p "$out"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

compile() {  # compile <output> <uv pip compile flags...>
  local dest="$1"; shift
  MACOSX_DEPLOYMENT_TARGET=14.0 "$UV" pip compile "$repo/pyproject.toml" "$@" \
    --python-version 3.12 --python-platform aarch64-apple-darwin --only-binary :all: \
    --no-header --no-annotate --quiet -o "$dest"
}

compile "$tmp/all.txt" --extra geo --extra rda --extra nc --extra hdf4 --generate-hashes
cp "$tmp/all.txt" "$out/macos-arm64-py312.txt"

# Package names per group, resolved against the full lock so versions agree.
names() { sed -n 's/^\([A-Za-z0-9_.-]*\)==.*/\1/p' "$1" | tr 'A-Z_.' 'a-z--' | sort -u; }
compile "$tmp/base.txt" -c "$tmp/all.txt"
for e in geo rda nc hdf4; do compile "$tmp/$e.txt" --extra "$e" -c "$tmp/all.txt"; done
{
  printf '{\n  "base": [%s]' "$(names "$tmp/base.txt" | sed 's/.*/"&"/' | paste -sd, -)"
  for e in geo rda nc hdf4; do
    printf ',\n  "%s": [%s]' "$e" "$(comm -13 <(names "$tmp/base.txt") <(names "$tmp/$e.txt") | sed 's/.*/"&"/' | paste -sd, -)"
  done
  printf '\n}\n'
} > "$out/macos-arm64-py312.groups.json"
echo "wrote $out/macos-arm64-py312.txt and .groups.json" >&2
