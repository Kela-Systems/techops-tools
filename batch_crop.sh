#!/bin/bash

# batch_crop.sh - Batch crop rasters for multiple sites
# Reads site names from a file, fetches their coordinates, and runs crop_raster.sh

set -e

SITES_FILE="${1:-context}"
RADIUS="${2:-5000}"
OUTPUT_BASE="${3:-site_outputs}"

if [[ ! -f "$SITES_FILE" ]]; then
    echo "Error: Sites file '$SITES_FILE' not found"
    exit 1
fi

echo "========================================"
echo "Batch Crop Rasters"
echo "========================================"
echo "Sites file: $SITES_FILE"
echo "Radius: $RADIUS meters"
echo "Output base: $OUTPUT_BASE"
echo "========================================"
echo ""

# Count total sites
total=$(grep -c . "$SITES_FILE" || echo "0")
current=0
failed=0

while IFS= read -r site || [[ -n "$site" ]]; do
    # Skip empty lines
    [[ -z "$site" ]] && continue
    
    ((current++))
    echo ""
    echo "========================================"
    echo "[$current/$total] Processing site: $site"
    echo "========================================"
    
    # Fetch coordinates from config-service
    url="https://${site}/config-service/system-settings/settings/default"
    echo "Fetching coordinates from: $url"
    
    location=$(curl -sS -X GET "$url" 2>/dev/null | jq -r '.ui_settings.map.default_location' 2>/dev/null)
    
    if [[ -z "$location" || "$location" == "null" ]]; then
        echo "WARNING: Could not fetch coordinates for $site, skipping..."
        ((failed++))
        continue
    fi
    
    echo "Location: $location"
    
    # Parse lat,lon (format is "lat,lon" which is "y,x")
    lat=$(echo "$location" | cut -d',' -f1)
    lon=$(echo "$location" | cut -d',' -f2)
    
    if [[ -z "$lat" || -z "$lon" ]]; then
        echo "WARNING: Could not parse coordinates for $site, skipping..."
        ((failed++))
        continue
    fi
    
    echo "Latitude (Y): $lat"
    echo "Longitude (X): $lon"
    
    # Create output directory for this site
    output_dir="${OUTPUT_BASE}/${site}"
    
    # Run crop_raster.sh
    echo "Running crop_raster.sh..."
    if ./crop_raster.sh -y "$lat" -x "$lon" -r "$RADIUS" -o "$output_dir"; then
        echo "SUCCESS: $site completed"
    else
        echo "ERROR: $site failed"
        ((failed++))
    fi
    
done < "$SITES_FILE"

echo ""
echo "========================================"
echo "Batch Processing Complete"
echo "========================================"
echo "Total sites: $total"
echo "Successful: $((current - failed))"
echo "Failed: $failed"
echo "Output directory: $OUTPUT_BASE"
