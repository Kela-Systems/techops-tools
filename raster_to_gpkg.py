#!/usr/bin/env python3
"""
Raster to GeoPackage (InspireCRS84Quad)
Usage:
    python raster_to_gpkg.py --input /path/to/tiffs --output /path/to/output.gpkg --zoom 16
"""

import argparse
import glob
import os
import subprocess
import sys
import tempfile

# ---------------------------------------------------------------------------
# Resolution lookup per zoom level (InspireCRS84Quad)
# ---------------------------------------------------------------------------
ZOOM_RESOLUTIONS = {
    0:  ("0.703125",            "0.703125"),
    1:  ("0.3515625",           "0.3515625"),
    2:  ("0.17578125",          "0.17578125"),
    3:  ("0.087890625",         "0.087890625"),
    4:  ("0.0439453125",        "0.0439453125"),
    5:  ("0.02197265625",       "0.02197265625"),
    6:  ("0.010986328125",      "0.010986328125"),
    7:  ("0.0054931640625",     "0.0054931640625"),
    8:  ("0.00274658203125",    "0.00274658203125"),
    9:  ("0.001373291015625",   "0.001373291015625"),
    10: ("0.0006866455078125",  "0.0006866455078125"),
    11: ("0.00034332275390625", "0.00034332275390625"),
    12: ("0.000171661376953125","0.000171661376953125"),
    13: ("8.58306884765625e-05","8.58306884765625e-05"),
    14: ("4.29153442382813e-05","4.29153442382813e-05"),
    15: ("2.14576721191406e-05","2.14576721191406e-05"),
    16: ("1.07288360595703e-05","1.07288360595703e-05"),
    17: ("5.36441802978516e-06","5.36441802978516e-06"),
    18: ("2.68220901489258e-06","2.68220901489258e-06"),
    19: ("1.34110450744629e-06","1.34110450744629e-06"),
    20: ("6.70552253723145e-07","6.70552253723145e-07"),
    21: ("3.35276126861572e-07","3.35276126861572e-07"),
}


def run_cmd(cmd: list, step: str):
    print(f"\n{'='*60}")
    print(f"  {step}")
    print(f"{'='*60}")
    print("CMD:", " ".join(cmd))
    print()
    subprocess.run(cmd, check=True)


def main():
    parser = argparse.ArgumentParser(
        description="Convert all TIFFs in a folder to an InspireCRS84Quad GeoPackage"
    )
    parser.add_argument(
        "--input", "-i", required=True,
        help="Folder containing *.tiff / *.tif files"
    )
    parser.add_argument(
        "--output", "-o", required=True,
        help="Output GeoPackage path (e.g. /path/to/output.gpkg)"
    )
    parser.add_argument(
        "--zoom", "-z", type=int, default=16, choices=range(0, 22),
        metavar="ZOOM",
        help="Zoom level 0-21 (default: 16 = 1.07e-05 deg resolution)"
    )
    parser.add_argument(
        "--cache-mb", type=int, default=4096,
        help="GDAL cache size in MB (default: 4096). Lower if RAM < 16GB."
    )
    args = parser.parse_args()

    # -----------------------------------------------------------------------
    # Find TIFFs
    # -----------------------------------------------------------------------
    tiffs = sorted(glob.glob(os.path.join(args.input, "*.tiff")))
    tiffs += sorted(glob.glob(os.path.join(args.input, "*.tif")))
    if not tiffs:
        print(f"ERROR: No .tiff/.tif files found in {args.input}")
        sys.exit(1)
    print(f"Found {len(tiffs)} raster file(s) in {args.input}")

    vrt_path = os.path.join(tempfile.gettempdir(), "mosaic_temp.vrt")
    tr_x, tr_y = ZOOM_RESOLUTIONS[args.zoom]

    try:
        # -------------------------------------------------------------------
        # Step 1 — Build Virtual Raster
        # -------------------------------------------------------------------
        run_cmd([
            "gdalbuildvrt",
            "-addalpha",
            "-srcnodata", "0",
            "-resolution", "highest",
            "-r", "average",
            vrt_path,
        ] + tiffs, step="Step 1/3 — Build Virtual Raster (VRT)")

        # -------------------------------------------------------------------
        # Step 2 — Warp to GeoPackage
        # -------------------------------------------------------------------
        run_cmd([
            "caffeinate", "-i",       # prevent macOS sleep
            "gdalwarp",
            "-overwrite",
            "-r", "average",
            "-of", "GPKG",
            "-co", "TILING_SCHEME=InspireCRS84Quad",
            "-co", "TILE_FORMAT=PNG_JPEG",
            "-srcnodata", "0 0 0",
            "-tr", tr_x, tr_y,
            "--config", "GDAL_CACHEMAX",    str(args.cache_mb),
            "--config", "GDAL_NUM_THREADS", "ALL_CPUS",
            "-multi",
            "-wo", "NUM_THREADS=ALL_CPUS",
            vrt_path,
            args.output,
        ], step="Step 2/3 — Warp to GeoPackage")

        # -------------------------------------------------------------------
        # Step 3 — Build Overviews
        # -------------------------------------------------------------------
        levels = [str(2 ** i) for i in range(1, args.zoom + 1)]
        run_cmd([
            "gdaladdo",
            "-r", "average",
            args.output,
        ] + levels, step="Step 3/3 — Build Overviews (Pyramids)")

    finally:
        if os.path.exists(vrt_path):
            os.remove(vrt_path)

    print(f"\n✓ Done! Output: {args.output}")


if __name__ == "__main__":
    main()