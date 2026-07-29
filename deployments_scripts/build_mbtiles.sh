#!/bin/bash

# build_mbtiles.sh - Build the C2 basemap (cuas.mbtiles) from a cropped orthophoto
#
# The C2 frontend renders the basemap from orthophoto/cuas.mbtiles (via the
# QGIS project's "cuas" layer), not from orthophoto.tif directly — so every
# site bundle needs one. crop_raster.sh produces the GeoTIFFs; this builds
# the tile pyramid.
#
# Usage: ./build_mbtiles.sh <orthophoto.tif> <output.mbtiles> [max_zoom]
#
#   max_zoom  Deepest tile level (default 17 ≈ 1 m/px at Israel's latitude;
#             18 quadruples size and build time)

set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <orthophoto.tif> <output.mbtiles> [max_zoom]"
    exit 1
fi

INPUT="$1"
OUTPUT="$2"
MAX_ZOOM="${3:-17}"

if [[ ! -f "$INPUT" ]]; then
    echo "Error: input '$INPUT' not found"
    exit 1
fi

command -v gdal_translate &> /dev/null || { echo "Error: gdal_translate not found. Please install GDAL."; exit 1; }

echo "Building MBTiles: $INPUT -> $OUTPUT (max zoom $MAX_ZOOM)"
rm -f "$OUTPUT"
gdal_translate -of MBTILES -co MAXZOOM="$MAX_ZOOM" "$INPUT" "$OUTPUT"

# Overview factors 2..128 add zoom levels MAX_ZOOM-1 .. MAX_ZOOM-7,
# so a max_zoom=17 build serves z10-z17.
echo "Adding overview zoom levels..."
gdaladdo -r average "$OUTPUT" 2 4 8 16 32 64 128

echo "Done:"
gdalinfo "$OUTPUT" | grep -E "Size is|ZOOM_LEVEL" || true
