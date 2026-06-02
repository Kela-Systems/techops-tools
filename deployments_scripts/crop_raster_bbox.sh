#!/bin/bash

# crop_raster_bbox.sh - Crop orthophoto and DTM to a bounding box or polygon
#
# Like crop_raster.sh, but instead of a center point + radius you specify the
# crop region directly. Three modes are supported:
#
#   1. Axis-aligned bbox       (-b xmin,ymin,xmax,ymax)
#   2. Bbox from corner points (-p "lat1,lon1 lat2,lon2 ...")
#   3. Polygon cutline         (-p ... --polygon  OR  --cutline <vector>)
#
# In polygon mode pixels outside the polygon become nodata (and transparent
# on the orthophoto via an alpha band, unless --no-alpha is passed).
#
# Usage:
#   ./crop_raster_bbox.sh [-i] \
#       [-b <xmin,ymin,xmax,ymax>] \
#       [-p "<lat1,lon1 lat2,lon2 ...>"] \
#       [--polygon] [--cutline <path>] [--no-alpha] [--no-crop-to-cutline] \
#       [-o <output_dir>] [--site <name>] [--upload]
#
# Options:
#   -i, --interactive       Interactive mode (prompt for bbox / points)
#   -b, --bbox              Explicit bounding box in EPSG:4326 as xmin,ymin,xmax,ymax
#                           (minLon,minLat,maxLon,maxLat -- gdalwarp -te order)
#   -p, --points            Quoted, whitespace-separated lat,lon points.
#                           Without --polygon: the axis-aligned bbox is used.
#                           With --polygon: the points form a polygon cutline.
#       --polygon           Treat -p points as polygon vertices and clip exactly
#                           (auto-closes the ring if not closed)
#       --cutline <path>    Use a vector file (GeoJSON / Shapefile / GPKG / ...)
#                           as the polygon cutline
#       --no-alpha          Do not add a transparency band to the orthophoto
#                           when cutline is active (default: alpha is added)
#       --no-crop-to-cutline Keep the bbox extent; mask outside but don't tighten
#                           (default: -crop_to_cutline shrinks output to polygon)
#   -o, --output            Output directory (default: tiff_output)
#       --site <name>       Create a site folder from template and move outputs there
#       --upload            Upload site folder to S3 (requires --site)
#   -h, --help              Show this help message

set -e

# Default input files (override via env vars or $CHATAL_MAPS_DIR)
CHATAL_MAPS_DIR="${CHATAL_MAPS_DIR:-$HOME/Documents/chatal-maps}"
INPUT_ORTHO="${INPUT_ORTHO:-$CHATAL_MAPS_DIR/template-site-map/orthophoto/raster.vrt}"
INPUT_DTM="${INPUT_DTM:-$CHATAL_MAPS_DIR/template-site-map/dtm/dtm_hae.tif}"

# Vertical-datum tag for the output DTM (set to "" to skip tagging).
#   EPSG:4979      = WGS 84 ellipsoidal (HAE)
#   EPSG:4326+3855 = WGS 84 + EGM2008 height (orthometric)
#   EPSG:4326+5773 = WGS 84 + EGM96 height (orthometric)
OUTPUT_DTM_VCRS="${OUTPUT_DTM_VCRS:-EPSG:4979}"

GIS_DATA_DIR="${GIS_DATA_DIR:-$HOME/Documents/kela-gis-data}"
SITE_TEMPLATE="${SITE_TEMPLATE:-$GIS_DATA_DIR/site-template}"
AWS_PROFILE="${AWS_PROFILE:-MapEditor-781540302536}"

usage() {
    echo "Usage: $0 [-i] [-b <xmin,ymin,xmax,ymax>] [-p \"<lat1,lon1 lat2,lon2 ...>\"]"
    echo "          [--polygon] [--cutline <path>] [--no-alpha] [--no-crop-to-cutline]"
    echo "          [-o <output_dir>] [--site <name>] [--upload]"
    echo ""
    echo "Crop orthophoto and DTM to a bounding box or arbitrary polygon."
    echo ""
    echo "Options:"
    echo "  -i, --interactive       Interactive mode (prompt for bbox / points)"
    echo "  -b, --bbox              Explicit bbox in EPSG:4326 as xmin,ymin,xmax,ymax"
    echo "                          (minLon,minLat,maxLon,maxLat -- gdalwarp -te order)"
    echo "  -p, --points            Quoted lat,lon points (whitespace-separated)"
    echo "      --polygon           Treat -p points as a polygon and clip exactly"
    echo "      --cutline <path>    Vector file (GeoJSON/Shapefile/GPKG/...) cutline"
    echo "      --no-alpha          Don't add transparency band to ortho in cutline mode"
    echo "      --no-crop-to-cutline Keep bbox extent, only mask outside polygon"
    echo "  -o, --output            Output directory (default: tiff_output)"
    echo "      --site <name>       Create a site folder from template and move outputs there"
    echo "      --upload            Upload site folder to S3 (requires --site)"
    echo "  -h, --help              Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 -b 35.0551,32.8905,35.6562,33.1446"
    echo "  $0 -p \"33.1134,35.0985 33.1446,35.6562 32.8952,35.6496 32.8905,35.0551\""
    echo "  $0 -p \"33.1134,35.0985 33.1446,35.6562 32.8952,35.6496 32.8905,35.0551\" --polygon"
    echo "  $0 --cutline ./roi.geojson"
    echo "  $0 -i --site my-site --upload"
    exit 1
}

# Function to crop a single raster. Supports either a -te bbox or a -cutline
# polygon; in cutline mode, the orthophoto can optionally get an alpha band.
# Globals: XMIN YMIN XMAX YMAX CUTLINE CROP_TO_CUTLINE ADD_ALPHA
crop_raster() {
    local input="$1"
    local output="$2"
    local name="$3"
    local resample="${4:-near}"
    local vcrs="${5:-}"
    local supports_alpha="${6:-false}"

    echo ""
    echo "========================================"
    echo "Processing: $name"
    echo "========================================"

    if [[ ! -f "$input" ]]; then
        echo "WARNING: Input file '$input' not found, skipping..."
        return 1
    fi

    echo "Input:  $input"
    echo "Output: $output"

    # Get source pixel size to preserve native resolution
    local pixel_line
    pixel_line=$(gdalinfo "$input" 2>/dev/null | grep "Pixel Size")
    local pixel_x=$(echo "$pixel_line" | sed 's/.*(\([^,]*\),\([^)]*\)).*/\1/')
    local pixel_y=$(echo "$pixel_line" | sed 's/.*(\([^,]*\),\([^)]*\)).*/\2/')
    pixel_x="${pixel_x#-}"
    pixel_y="${pixel_y#-}"

    echo "Source pixel size: $pixel_x x $pixel_y"
    echo ""

    mkdir -p "$(dirname "$output")"

    # Build extent / cutline arguments
    local extent_args=()
    if [[ -n "$CUTLINE" ]]; then
        extent_args+=( -cutline "$CUTLINE" )
        if [[ "$CROP_TO_CUTLINE" == true ]]; then
            extent_args+=( -crop_to_cutline )
        else
            extent_args+=( -te "$XMIN" "$YMIN" "$XMAX" "$YMAX" -te_srs EPSG:4326 )
        fi
        echo "Cutline: $CUTLINE  (crop_to_cutline=$CROP_TO_CUTLINE)"
    else
        extent_args+=( -te "$XMIN" "$YMIN" "$XMAX" "$YMAX" -te_srs EPSG:4326 )
    fi

    # Optional alpha band (orthophoto in cutline mode)
    local alpha_args=()
    if [[ -n "$CUTLINE" && "$supports_alpha" == "true" && "$ADD_ALPHA" == "true" ]]; then
        alpha_args+=( -dstalpha )
    fi

    gdalwarp \
        -overwrite \
        "${extent_args[@]}" \
        -tr "$pixel_x" "$pixel_y" \
        -r "$resample" \
        -co COMPRESS=LZW \
        -co TILED=YES \
        -co BIGTIFF=IF_SAFER \
        -multi -wo NUM_THREADS=ALL_CPUS \
        --config GDAL_CACHEMAX 2048 \
        "${alpha_args[@]}" \
        "$input" \
        "$output"

    if [[ -n "$vcrs" ]]; then
        if command -v gdal_edit.py &> /dev/null; then
            gdal_edit.py -a_srs "$vcrs" "$output"
            echo "Tagged CRS: $vcrs"
        else
            echo "WARNING: gdal_edit.py not found; skipping CRS tag ($vcrs)"
        fi
    fi

    echo "Done: $output"

    if command -v gdalinfo &> /dev/null; then
        OUTPUT_INFO=$(gdalinfo -stats "$output" 2>/dev/null)
        SIZE=$(echo "$OUTPUT_INFO" | grep "^Size" | head -1)
        PIXEL=$(echo "$OUTPUT_INFO" | grep "Pixel Size" | head -1)
        echo "$SIZE"
        echo "$PIXEL"
    fi
}

# Compute axis-aligned bbox from a whitespace-separated list of "lat,lon" tokens.
# Sets BBOX_INPUT (xmin,ymin,xmax,ymax) on success.
bbox_from_points() {
    local raw="$1"
    if [[ -z "$raw" ]]; then
        echo "Error: empty points list"
        exit 1
    fi
    BBOX_INPUT=$(awk -v s="$raw" 'BEGIN {
        n = split(s, toks, /[[:space:]]+/)
        first = 1
        for (i = 1; i <= n; i++) {
            t = toks[i]
            if (t == "") continue
            if (split(t, p, ",") != 2) {
                print "ERR:bad token: " t > "/dev/stderr"; exit 2
            }
            lat = p[1] + 0; lon = p[2] + 0
            if (first) { minLat=lat; maxLat=lat; minLon=lon; maxLon=lon; first=0 }
            else {
                if (lat < minLat) minLat = lat
                if (lat > maxLat) maxLat = lat
                if (lon < minLon) minLon = lon
                if (lon > maxLon) maxLon = lon
            }
        }
        if (first) { print "ERR:no points" > "/dev/stderr"; exit 2 }
        printf "%.10g,%.10g,%.10g,%.10g\n", minLon, minLat, maxLon, maxLat
    }') || { echo "Error: failed to parse points"; exit 1; }
}

# Parse command line arguments
INTERACTIVE=false
UPLOAD=false
SITE_NAME=""
BBOX_INPUT=""
POINTS_INPUT=""
USE_POLYGON=false
CUTLINE=""
ADD_ALPHA=true
CROP_TO_CUTLINE=true

while [[ $# -gt 0 ]]; do
    case $1 in
        -i|--interactive)
            INTERACTIVE=true
            shift
            ;;
        -b|--bbox)
            [[ -z "$2" || "$2" == -* ]] && { echo "Error: $1 requires a value"; exit 1; }
            BBOX_INPUT="$2"
            shift 2
            ;;
        -p|--points)
            [[ -z "$2" ]] && { echo "Error: $1 requires a value"; exit 1; }
            POINTS_INPUT="$2"
            shift 2
            ;;
        --polygon)
            USE_POLYGON=true
            shift
            ;;
        --cutline)
            [[ -z "$2" || "$2" == -* ]] && { echo "Error: $1 requires a value"; exit 1; }
            CUTLINE="$2"
            shift 2
            ;;
        --no-alpha)
            ADD_ALPHA=false
            shift
            ;;
        --no-crop-to-cutline)
            CROP_TO_CUTLINE=false
            shift
            ;;
        -o|--output)
            [[ -z "$2" || "$2" == -* ]] && { echo "Error: $1 requires a value"; exit 1; }
            OUTPUT_DIR="$2"
            shift 2
            ;;
        --site)
            [[ -z "$2" || "$2" == -* ]] && { echo "Error: $1 requires a value"; exit 1; }
            SITE_NAME="$2"
            shift 2
            ;;
        --upload)
            UPLOAD=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Error: Unknown option $1"
            usage
            ;;
    esac
done

# Interactive mode: prompt for missing values
if [[ "$INTERACTIVE" == true ]]; then
    if [[ -z "$BBOX_INPUT" && -z "$POINTS_INPUT" ]]; then
        echo "Provide either an explicit bbox or a set of corner points:"
        read -p "Bbox xmin,ymin,xmax,ymax (leave empty to enter points): " BBOX_INPUT
        if [[ -z "$BBOX_INPUT" ]]; then
            read -p "Points (lat1,lon1 lat2,lon2 ...): " POINTS_INPUT
        fi
    fi
    if [[ -z "$SITE_NAME" ]]; then
        read -p "Site name (leave empty to skip): " SITE_NAME
    fi
fi

# Resolve bbox (from points if needed)
if [[ -n "$POINTS_INPUT" && -n "$BBOX_INPUT" ]]; then
    echo "Error: --bbox and --points are mutually exclusive"
    exit 1
fi
if [[ -n "$POINTS_INPUT" ]]; then
    bbox_from_points "$POINTS_INPUT"
fi
if [[ -z "$BBOX_INPUT" ]]; then
    echo "Error: missing --bbox or --points"
    usage
fi

# Parse and validate bbox: xmin,ymin,xmax,ymax
BBOX_INPUT="${BBOX_INPUT// /}"
IFS=',' read -r XMIN YMIN XMAX YMAX <<< "$BBOX_INPUT"
if [[ -z "$XMIN" || -z "$YMIN" || -z "$XMAX" || -z "$YMAX" ]]; then
    echo "Error: bbox must be xmin,ymin,xmax,ymax (got: $BBOX_INPUT)"
    exit 1
fi
for v in "$XMIN" "$YMIN" "$XMAX" "$YMAX"; do
    if ! [[ "$v" =~ ^-?[0-9]+\.?[0-9]*([eE][-+]?[0-9]+)?$ ]]; then
        echo "Error: bbox value must be numeric (got: $v)"
        exit 1
    fi
done
# Sanity: ensure min < max (auto-swap with a warning)
if awk "BEGIN { exit !($XMIN > $XMAX) }"; then
    echo "WARNING: xmin > xmax, swapping"
    tmp="$XMIN"; XMIN="$XMAX"; XMAX="$tmp"
fi
if awk "BEGIN { exit !($YMIN > $YMAX) }"; then
    echo "WARNING: ymin > ymax, swapping"
    tmp="$YMIN"; YMIN="$YMAX"; YMAX="$tmp"
fi

# Cutline / polygon resolution
if [[ "$USE_POLYGON" == true && -n "$CUTLINE" ]]; then
    echo "Error: --polygon and --cutline are mutually exclusive"
    exit 1
fi
if [[ "$USE_POLYGON" == true ]]; then
    if [[ -z "$POINTS_INPUT" ]]; then
        echo "Error: --polygon requires -p with at least 3 points"
        exit 1
    fi
    CUTLINE="${TMPDIR:-/tmp}/crop_raster_polygon.$$.geojson"
    awk -v s="$POINTS_INPUT" -v out="$CUTLINE" 'BEGIN {
        n = split(s, toks, /[[:space:]]+/)
        m = 0
        for (i = 1; i <= n; i++) {
            t = toks[i]; if (t == "") continue
            if (split(t, p, ",") != 2) {
                print "Error: bad point token: " t > "/dev/stderr"; exit 2
            }
            m++
            lats[m] = p[1] + 0
            lons[m] = p[2] + 0
        }
        if (m < 3) { print "Error: --polygon needs at least 3 points (got " m ")" > "/dev/stderr"; exit 2 }
        # auto-close ring
        if (lats[m] != lats[1] || lons[m] != lons[1]) {
            m++; lats[m] = lats[1]; lons[m] = lons[1]
        }
        printf "{\n" > out
        printf "  \"type\": \"FeatureCollection\",\n" > out
        printf "  \"features\": [{\n" > out
        printf "    \"type\": \"Feature\", \"properties\": {},\n" > out
        printf "    \"geometry\": { \"type\": \"Polygon\", \"coordinates\": [[\n" > out
        for (i = 1; i <= m; i++) {
            sep = (i < m) ? "," : ""
            printf "      [%.10g, %.10g]%s\n", lons[i], lats[i], sep > out
        }
        printf "    ]] }\n" > out
        printf "  }]\n" > out
        printf "}\n" > out
    }'
    # `set -e` aborts on awk failure; stderr already carries the reason.
    echo "Generated polygon cutline: $CUTLINE"
    trap 'rm -f "$CUTLINE"' EXIT
fi
if [[ -n "$CUTLINE" && ! -f "$CUTLINE" ]]; then
    echo "Error: cutline file not found: $CUTLINE"
    exit 1
fi

# Default output directory
OUTPUT_DIR="${OUTPUT_DIR:-tiff_output}"

# --upload requires --site
if [[ "$UPLOAD" == true && -z "$SITE_NAME" ]]; then
    echo "Error: --upload requires --site"
    exit 1
fi

# Check dependencies
if ! command -v gdalwarp &> /dev/null; then
    echo "Error: gdalwarp not found. Please install GDAL."
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Approximate area report (so the user knows what they're about to crop)
read -r AREA_KM_W AREA_KM_H <<< "$(awk -v xmin="$XMIN" -v ymin="$YMIN" -v xmax="$XMAX" -v ymax="$YMAX" \
    'BEGIN {
        midLat = (ymin + ymax) / 2
        kmLat = 111.320
        kmLon = 111.320 * cos(midLat * 3.14159265359 / 180)
        printf "%.2f %.2f", (xmax - xmin) * kmLon, (ymax - ymin) * kmLat
     }')"

echo "========================================"
echo "Crop Parameters"
echo "========================================"
echo "Bounding box: $XMIN $YMIN $XMAX $YMAX  (xmin ymin xmax ymax, EPSG:4326)"
echo "Approx size:  ${AREA_KM_W} km x ${AREA_KM_H} km"
if [[ -n "$CUTLINE" ]]; then
    echo "Mode: polygon clip (cutline: $CUTLINE)"
else
    echo "Mode: bbox clip"
fi
echo "Output directory: $OUTPUT_DIR"

ORTHO_OK=false
DTM_OK=false
crop_raster "$INPUT_ORTHO" "$OUTPUT_DIR/orthophoto/orthophoto.tif" "Orthophoto" "near"     ""                  "true"  && ORTHO_OK=true || true
crop_raster "$INPUT_DTM"   "$OUTPUT_DIR/dtm/dtm.tif"               "DTM"        "bilinear" "$OUTPUT_DTM_VCRS"  "false" && DTM_OK=true   || true

echo ""
echo "========================================"
echo "Cropping complete!"
echo "========================================"
echo "Output files:"
echo "  - $OUTPUT_DIR/orthophoto/orthophoto.tif"
echo "  - $OUTPUT_DIR/dtm/dtm.tif"

# Site provisioning
if [[ -n "$SITE_NAME" ]]; then
    SITE_DIR="$GIS_DATA_DIR/$SITE_NAME"

    echo ""
    echo "========================================"
    echo "Setting up site: $SITE_NAME"
    echo "========================================"

    if [[ -d "$SITE_DIR" ]]; then
        echo "WARNING: Site directory '$SITE_DIR' already exists, skipping template copy"
    else
        if [[ ! -d "$SITE_TEMPLATE" ]]; then
            echo "Error: Site template not found at '$SITE_TEMPLATE'"
            exit 1
        fi
        cp -r "$SITE_TEMPLATE" "$SITE_DIR"
        echo "Created site from template: $SITE_DIR"
    fi

    if [[ "$ORTHO_OK" == true ]]; then
        mv "$OUTPUT_DIR/orthophoto/orthophoto.tif" "$SITE_DIR/orthophoto/"
        echo "Moved orthophoto -> $SITE_DIR/orthophoto/"
    else
        echo "WARNING: Orthophoto was not cropped, skipping move"
    fi
    if [[ "$DTM_OK" == true ]]; then
        mv "$OUTPUT_DIR/dtm/dtm.tif" "$SITE_DIR/dtm/"
        echo "Moved dtm -> $SITE_DIR/dtm/"
    else
        echo "WARNING: DTM was not cropped, skipping move"
    fi
fi

# Upload to S3
if [[ "$UPLOAD" == true ]]; then
    echo ""
    echo "========================================"
    echo "Uploading to S3"
    echo "========================================"

    aws sso login --profile "$AWS_PROFILE"
    aws s3 sync "$SITE_DIR" "s3://kela-gis-data/$SITE_NAME" --profile "$AWS_PROFILE"

    echo "Upload complete: s3://kela-gis-data/$SITE_NAME"
fi

echo ""
echo "========================================"
echo "All done!"
echo "========================================"
