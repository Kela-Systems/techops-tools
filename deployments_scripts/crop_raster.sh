#!/bin/bash

# crop_raster.sh - Crop orthophoto and DTM to a square region around a center point
#
# Usage: ./crop_raster.sh [-i] [-c <lat,lon>] [-r <radius>] [-o <output_dir>] [--site <name>] [--upload] [--tar]
#
# Options:
#   -i, --interactive  Interactive mode (prompt for coordinates and radius)
#   -c, --center       Center coordinates as lat,lon (e.g. 32.104136,35.529141 or "32.104136, 35.529141")
#   -r, --radius       Radius in meters
#   -o, --output       Output directory (default: tiff_output)
#   --site <name>      Create a site folder from template and move outputs there
#   --upload           Upload site folder to S3 (requires --site)
#   --tar              Create a .tar.gz archive of the site folder (requires --site)
#   -h, --help         Show this help message

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
    echo "Usage: $0 [-i] [-c <lat,lon>] [-r <radius>] [-o <output_dir>] [--site <name>] [--upload] [--tar]"
    echo ""
    echo "Crop orthophoto and DTM to a square region around a center point."
    echo ""
    echo "Options:"
    echo "  -i, --interactive  Interactive mode (prompt for coordinates and radius)"
    echo "  -c, --center       Center coordinates as lat,lon (e.g. 32.104136,35.529141)"
    echo "  -r, --radius       Radius in meters"
    echo "  -o, --output       Output directory (default: tiff_output)"
    echo "  --site <name>      Create a site folder from template and move outputs there"
    echo "  --upload           Upload site folder to S3 (requires --site)"
    echo "  --tar              Create a .tar.gz archive of the site folder (requires --site)"
    echo "  -h, --help         Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 -i"
    echo "  $0 -c 32.869847,35.698983 -r 1000"
    echo "  $0 -c \"32.869847, 35.698983\" -r 1000"
    echo "  $0 -i --site my-site --upload"
    echo "  $0 -c 32.869847,35.698983 -r 1000 --site my-site --tar"
    exit 1
}

# Function to crop a single raster
crop_raster() {
    local input="$1"
    local output="$2"
    local name="$3"
    local resample="${4:-near}"
    local vcrs="${5:-}"

    echo ""
    echo "========================================"
    echo "Processing: $name"
    echo "========================================"
    
    # Check if input file exists
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
    
    # Create output directory if needed
    mkdir -p "$(dirname "$output")"
    
    gdalwarp \
        -overwrite \
        -te "$XMIN" "$YMIN" "$XMAX" "$YMAX" \
        -te_srs EPSG:4326 \
        -tr "$pixel_x" "$pixel_y" \
        -r "$resample" \
        -co COMPRESS=LZW \
        -co TILED=YES \
        "$input" \
        "$output"

    # Tag the output with a vertical CRS if one was requested
    if [[ -n "$vcrs" ]]; then
        if command -v gdal_edit.py &> /dev/null; then
            gdal_edit.py -a_srs "$vcrs" "$output"
            echo "Tagged CRS: $vcrs"
        else
            echo "WARNING: gdal_edit.py not found; skipping CRS tag ($vcrs)"
        fi
    fi

    echo "Done: $output"

    # Show brief output info
    if command -v gdalinfo &> /dev/null; then
        OUTPUT_INFO=$(gdalinfo -stats "$output" 2>/dev/null)
        SIZE=$(echo "$OUTPUT_INFO" | grep "^Size" | head -1)
        PIXEL=$(echo "$OUTPUT_INFO" | grep "Pixel Size" | head -1)
        echo "$SIZE"
        echo "$PIXEL"
    fi
}

# Parse command line arguments
INTERACTIVE=false
UPLOAD=false
TARBALL=false
SITE_NAME=""

while [[ $# -gt 0 ]]; do
    case $1 in
        -i|--interactive)
            INTERACTIVE=true
            shift
            ;;
        -c|--center)
            [[ -z "$2" || "$2" == -* ]] && { echo "Error: $1 requires a value"; exit 1; }
            CENTER_INPUT="$2"
            shift 2
            if [[ -n "$1" && "$1" != -* && "$CENTER_INPUT" == *, ]]; then
                CENTER_INPUT="${CENTER_INPUT}$1"
                shift
            fi
            ;;
        -r|--radius)
            [[ -z "$2" || "$2" == -* ]] && { echo "Error: $1 requires a value"; exit 1; }
            RADIUS="$2"
            shift 2
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
        --tar)
            TARBALL=true
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
    if [[ -z "$CENTER_INPUT" ]]; then
        read -p "Center lat,lon (e.g. 32.104136,35.529141): " CENTER_INPUT
    fi
    if [[ -z "$RADIUS" ]]; then
        read -p "Radius in meters: " RADIUS
    fi
    if [[ -z "$SITE_NAME" ]]; then
        read -p "Site name (leave empty to skip): " SITE_NAME
    fi
fi

# Parse and validate center coordinates (lat,lon)
if [[ -n "$CENTER_INPUT" ]]; then
    CENTER_INPUT="${CENTER_INPUT// /}"
    if [[ "$CENTER_INPUT" != *,* ]]; then
        echo "Error: Center coordinates must be in lat,lon format (e.g. 32.104136,35.529141)"
        exit 1
    fi
    CENTER_Y="${CENTER_INPUT%%,*}"
    CENTER_X="${CENTER_INPUT##*,}"
    if ! [[ "$CENTER_Y" =~ ^-?[0-9]+\.?[0-9]*$ && "$CENTER_X" =~ ^-?[0-9]+\.?[0-9]*$ ]]; then
        echo "Error: Center coordinates must be numeric (got: $CENTER_Y,$CENTER_X)"
        exit 1
    fi
fi

# Default output directory
OUTPUT_DIR="${OUTPUT_DIR:-tiff_output}"

# Validate required arguments
if [[ -z "$CENTER_X" || -z "$CENTER_Y" || -z "$RADIUS" ]]; then
    echo "Error: Missing required arguments (center coordinates, radius)"
    usage
fi

# --upload requires --site
if [[ "$UPLOAD" == true && -z "$SITE_NAME" ]]; then
    echo "Error: --upload requires --site"
    exit 1
fi

# --tar requires --site
if [[ "$TARBALL" == true && -z "$SITE_NAME" ]]; then
    echo "Error: --tar requires --site"
    exit 1
fi

# Check dependencies
if ! command -v gdalwarp &> /dev/null; then
    echo "Error: gdalwarp not found. Please install GDAL."
    exit 1
fi
if ! command -v bc &> /dev/null; then
    echo "Error: bc not found. Please install bc."
    exit 1
fi

# Create output directory if it doesn't exist
mkdir -p "$OUTPUT_DIR"

# Convert radius from meters to degrees
# Approximate conversion:
# 1 degree latitude ≈ 111320 meters
# 1 degree longitude ≈ 111320 * cos(latitude) meters
RADIUS_LAT=$(echo "scale=10; $RADIUS / 111320" | bc)
RADIUS_LON=$(echo "scale=10; $RADIUS / (111320 * c($CENTER_Y * 3.14159265359 / 180))" | bc -l)

# Calculate bounding box
XMIN=$(echo "scale=10; $CENTER_X - $RADIUS_LON" | bc)
XMAX=$(echo "scale=10; $CENTER_X + $RADIUS_LON" | bc)
YMIN=$(echo "scale=10; $CENTER_Y - $RADIUS_LAT" | bc)
YMAX=$(echo "scale=10; $CENTER_Y + $RADIUS_LAT" | bc)

echo "========================================"
echo "Crop Parameters"
echo "========================================"
echo "Center: $CENTER_X, $CENTER_Y"
echo "Radius: $RADIUS meters"
echo "Bounding box: $XMIN $YMIN $XMAX $YMAX"
echo "Output directory: $OUTPUT_DIR"

# Crop both rasters (|| true prevents set -e from aborting on missing inputs)
ORTHO_OK=false
DTM_OK=false
crop_raster "$INPUT_ORTHO" "$OUTPUT_DIR/orthophoto/orthophoto.tif" "Orthophoto" "near" && ORTHO_OK=true || true
crop_raster "$INPUT_DTM" "$OUTPUT_DIR/dtm/dtm.tif" "DTM" "bilinear" "$OUTPUT_DTM_VCRS" && DTM_OK=true || true

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

# Create .tar.gz archive of the site folder
if [[ "$TARBALL" == true ]]; then
    TARBALL_PATH="$GIS_DATA_DIR/$SITE_NAME.tar.gz"

    echo ""
    echo "========================================"
    echo "Creating archive"
    echo "========================================"

    tar -czf "$TARBALL_PATH" --exclude ".DS_Store" -C "$SITE_DIR" .
    echo "Archive created: $TARBALL_PATH"
fi

# Upload to S3
if [[ "$UPLOAD" == true ]]; then
    echo ""
    echo "========================================"
    echo "Uploading to S3"
    echo "========================================"

    aws sso login --profile "$AWS_PROFILE"
    aws s3 sync "$SITE_DIR" "s3://kela-gis-data/$SITE_NAME" --profile "$AWS_PROFILE" --exclude "*.DS_Store"

    echo "Upload complete: s3://kela-gis-data/$SITE_NAME"
fi

echo ""
echo "========================================"
echo "All done!"
echo "========================================"
