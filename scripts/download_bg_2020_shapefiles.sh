#!/usr/bin/env bash
#
# Download TIGER/Line 2024 Block Group shapefiles (2020 BG vintage).
#
# These are used by configs/c3/bg_us_2020_demo.yaml to build BG weights aligned
# with the new NDI dataset's `bg_fips_2020` field (ACS 5-year periods with end
# year >= 2020 use 2020 Census BG boundaries; earlier periods use 2010 BGs and
# are handled by the existing configs/c3/bg_us_demo.yaml).
#
# Run from the exposure-data root:
#   bash scripts/download_bg_2020_shapefiles.sh
#
# Coverage : 50 states + DC (51 entries; territories PR/VI/GU/AS/MP excluded
#            to match the 2010 BG config — add if your cohort needs them)
# Source   : https://www2.census.gov/geo/tiger/TIGER2024/BG/
# Total DL : ~750 MB (51 zips). Disk after extract: ~1.5 GB.
# Idempotent: re-running skips states that are already extracted.

set -euo pipefail

YEAR=2024
TIGER_BASE="https://www2.census.gov/geo/tiger/TIGER${YEAR}/BG"
LOCAL_BASE="BG/C3/tiger${YEAR}_bg_states"

# 50 states + DC (FIPS codes 03/07/14/43/52 are reserved/unused)
STATES=(
    01 02 04 05 06 08 09 10 11 12 13 15 16 17 18 19 20 21 22 23 24 25
    26 27 28 29 30 31 32 33 34 35 36 37 38 39 40 41 42 44 45 46 47 48
    49 50 51 53 54 55 56
)

# Sanity: must run from the exposure-data root (the dir holding BG/, Noise/, …)
if [ ! -d "BG" ]; then
    echo "ERROR: 'BG/' not found in $(pwd)." >&2
    echo "       Run this script from the exposure-data root." >&2
    exit 1
fi

# Required tools
for tool in curl unzip; do
    command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: '$tool' not found in PATH." >&2; exit 1; }
done

mkdir -p "$LOCAL_BASE"

ok=0
skip=0
fail=0
echo "=== Downloading TIGER ${YEAR} BG (2020 vintage) for ${#STATES[@]} states ==="
for STATE in "${STATES[@]}"; do
    NAME="tl_${YEAR}_${STATE}_bg"
    ZIP="$LOCAL_BASE/${NAME}.zip"
    DIR="$LOCAL_BASE/${NAME}"
    SHP="$DIR/${NAME}.shp"
    URL="${TIGER_BASE}/${NAME}.zip"

    if [ -f "$SHP" ]; then
        skip=$((skip + 1))
        printf "  [%2d/51] %s  ✓ already extracted\n" "$((ok + skip + fail))" "$STATE"
        continue
    fi

    printf "  [%2d/51] %s  → %s\n" "$((ok + skip + fail + 1))" "$STATE" "$URL"
    if ! curl -fsSL -o "$ZIP" "$URL"; then
        echo "    ✗ download failed for state $STATE" >&2
        fail=$((fail + 1))
        continue
    fi
    mkdir -p "$DIR"
    if ! unzip -q -o "$ZIP" -d "$DIR"; then
        echo "    ✗ unzip failed for state $STATE" >&2
        fail=$((fail + 1))
        rm -f "$ZIP"
        continue
    fi
    rm -f "$ZIP"
    ok=$((ok + 1))
done

echo
echo "=== Summary ==="
echo "  newly downloaded : $ok"
echo "  already present  : $skip"
echo "  failed           : $fail"
echo "  destination      : $LOCAL_BASE"
du -sh "$LOCAL_BASE" 2>/dev/null || true

if [ "$fail" -gt 0 ]; then
    echo
    echo "⚠️  $fail state(s) failed. Re-run this script to retry (idempotent)." >&2
    exit 1
fi

# Show field schema of one shapefile to confirm GEOID column name
echo
SAMPLE="$LOCAL_BASE/tl_${YEAR}_06_bg/tl_${YEAR}_06_bg.shp"  # CA
if [ -f "$SAMPLE" ] && command -v ogrinfo >/dev/null 2>&1; then
    echo "=== Sample schema (CA, ${SAMPLE}) — confirm join_col in config matches ==="
    ogrinfo -so "$SAMPLE" "$(basename "$SAMPLE" .shp)" 2>/dev/null | grep -E '^(GEOID|STATEFP|COUNTYFP)' | head -5
fi

echo
echo "Done. Now run:"
echo "  spacescans run configs/c3/bg_us_2020_demo.yaml"
