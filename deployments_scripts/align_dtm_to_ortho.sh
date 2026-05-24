#!/bin/bash

# align_dtm_to_ortho.sh - Create a DTM that matches an orthophoto's extent + CRS,
#                        keeping the DTM's native pixel size.
#
# Use the orthophoto as a "shape template": the output DTM covers the exact
# same geographic bounding box and uses the same CRS, but retains the DTM's
# native resolution (no upsampling to the ortho's pixel size).
#
# Usage:
#   ./align_dtm_to_ortho.sh -o <orthophoto> -d <input_dtm> -t <output_dtm>
#   ./align_dtm_to_ortho.sh <orthophoto> <input_dtm> <output_dtm>     (positional)
#
# Options:
#   -o, --ortho      Reference orthophoto (extent + CRS template)
#   -d, --dtm        Input DTM (any CRS/resolution; will be reprojected if needed)
#   -t, --target     Output DTM path
#   -r, --resample   Resampling method (default: bilinear)
#   -h, --help       Show this help message

set -e

usage() {
    cat <<EOF
Usage: $0 -o <orthophoto> -d <input_dtm> -t <output_dtm> [-r <resample>]

Create a DTM that covers the same bbox + CRS as an orthophoto, preserving the
DTM's native pixel size.

Options:
  -o, --ortho      Reference orthophoto (extent + CRS template)
  -d, --dtm        Input DTM
  -t, --target     Output DTM path
  -r, --resample   Resampling method: bilinear (default), cubic, near, average
  -h, --help       Show this help message

Examples:
  $0 -o site/orthophoto.tif -d dtm_hae.tif -t site/dtm.tif
  $0 site/orthophoto.tif dtm_hae.tif site/dtm.tif
EOF
    exit 1
}

RESAMPLE="bilinear"

# Parse args (supports both flagged and positional)
POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -o|--ortho)   ORTHO="$2"; shift 2 ;;
        -d|--dtm)     DTM_IN="$2"; shift 2 ;;
        -t|--target)  DTM_OUT="$2"; shift 2 ;;
        -r|--resample) RESAMPLE="$2"; shift 2 ;;
        -h|--help)    usage ;;
        -*)           echo "Unknown option: $1"; usage ;;
        *)            POSITIONAL+=("$1"); shift ;;
    esac
done

# Fall back to positional args if flags weren't used
if [[ ${#POSITIONAL[@]} -eq 3 ]]; then
    ORTHO="${ORTHO:-${POSITIONAL[0]}}"
    DTM_IN="${DTM_IN:-${POSITIONAL[1]}}"
    DTM_OUT="${DTM_OUT:-${POSITIONAL[2]}}"
fi

# Validate
if [[ -z "$ORTHO" || -z "$DTM_IN" || -z "$DTM_OUT" ]]; then
    echo "Error: missing required arguments"
    usage
fi
if [[ ! -f "$ORTHO" ]]; then
    echo "Error: orthophoto '$ORTHO' not found"; exit 1
fi
if [[ ! -f "$DTM_IN" ]]; then
    echo "Error: input DTM '$DTM_IN' not found"; exit 1
fi
if ! command -v gdalwarp &>/dev/null; then
    echo "Error: gdalwarp not found"; exit 1
fi

# Extract orthophoto extent (in its native CRS)
eval "$(gdalinfo "$ORTHO" | awk '
    /Upper Left/  {gsub(/[(),]/,""); printf "ULX=%s\nULY=%s\n", $3, $4}
    /Lower Right/ {gsub(/[(),]/,""); printf "LRX=%s\nLRY=%s\n", $3, $4}')"

if [[ -z "$ULX" || -z "$ULY" || -z "$LRX" || -z "$LRY" ]]; then
    echo "Error: could not parse orthophoto extent"; exit 1
fi

# Extract orthophoto CRS as WKT (target CRS for the output DTM)
T_SRS=$(gdalsrsinfo -o wkt "$ORTHO" | tr -d '\n')
if [[ -z "$T_SRS" ]]; then
    echo "Error: orthophoto has no CRS"; exit 1
fi

# Extract DTM's native pixel size (absolute values)
PIXEL_LINE=$(gdalinfo "$DTM_IN" | grep "Pixel Size")
PX=$(echo "$PIXEL_LINE" | sed 's/.*(\([^,]*\),\([^)]*\)).*/\1/' | tr -d '-')
PY=$(echo "$PIXEL_LINE" | sed 's/.*(\([^,]*\),\([^)]*\)).*/\2/' | tr -d '-')

if [[ -z "$PX" || -z "$PY" ]]; then
    echo "Error: could not parse DTM pixel size"; exit 1
fi

echo "========================================"
echo "Aligning DTM to orthophoto"
echo "========================================"
echo "Orthophoto:      $ORTHO"
echo "Input DTM:       $DTM_IN"
echo "Output DTM:      $DTM_OUT"
echo "Extent (ortho):  $ULX $LRY $LRX $ULY"
echo "DTM pixel size:  $PX x $PY  (native, preserved)"
echo "Resampling:      $RESAMPLE"
echo ""

mkdir -p "$(dirname "$DTM_OUT")"

gdalwarp -overwrite \
    -t_srs "$T_SRS" \
    -te "$ULX" "$LRY" "$LRX" "$ULY" \
    -tr "$PX" "$PY" \
    -r "$RESAMPLE" \
    -co COMPRESS=LZW \
    -co TILED=YES \
    "$DTM_IN" "$DTM_OUT"

echo ""
echo "========================================"
echo "Done. Output summary:"
echo "========================================"
gdalinfo "$DTM_OUT" | grep -E "Size is|Pixel Size|Upper Left|Lower Right|Coordinate System is:"
