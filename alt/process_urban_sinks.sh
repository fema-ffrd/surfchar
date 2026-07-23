#!/bin/bash

#####################################################################
# Urban Areas Sink Analysis Script
# 
# Purpose: Process DEM and flow accumulation data to identify and
# analyze sinks in urban areas. This script implements the workflow
# from "2026-07-23_1052 Urban Areas.md"
#
# Joins (NSI, building class, etc.) are NOT performed by this script
#####################################################################

set -euo pipefail

# Configuration
S3_BUCKET="arc-urban-areas-894169284666-us-west-2-an"
WORK_DIR="${WORK_DIR:-.}"
VERBOSE="${VERBOSE:=0}"

# Ordered list of cities to process
CITIES_ORDERED=(
    fargo
    knoxville
    nashville
    jacksonville
    denver
    kansas-city
    pittsburgh
    seattle
    tampa
    minneapolis
    phoenix
    dallas
    atlanta
)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

#####################################################################
# Utility Functions
#####################################################################

log() {
    echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $*" >&2
}

error() {
    echo -e "${RED}[ERROR]${NC} $*" >&2
    exit 1
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $*" >&2
}

debug() {
    if [[ $VERBOSE -eq 1 ]]; then
        echo -e "${YELLOW}[DEBUG]${NC} $*" >&2
    fi
}

check_command() {
    local cmd=$1
    if ! command -v "$cmd" &> /dev/null; then
        error "Required command not found: $cmd"
    fi
}

#####################################################################
# Prerequisites
#####################################################################

check_prerequisites() {
    log "Checking prerequisites..."
    check_command "aws"
    check_command "gdal_calc.py"
    check_command "gdal_polygonize.py"
    check_command "gdal_rasterize"
    check_command "gdal_translate"
    check_command "ogr2ogr"
    check_command "gdalinfo"
    log "All prerequisites found"
}

#####################################################################
# S3 Operations
#####################################################################

download_from_s3() {
    local s3_path=$1
    local local_path=$2
    
    if [[ -f "$local_path" ]]; then
        log "File already exists: $local_path (skipping download)"
        return 0
    fi
    
    log "Downloading from S3: $s3_path -> $local_path"
    aws s3 cp "s3://${S3_BUCKET}/${s3_path}" "$local_path" || error "Failed to download $s3_path"
}

sync_to_s3() {
    local local_dir=$1
    local s3_path=$2
    
    if [[ ! -d "$local_dir" ]]; then
        error "Directory not found: $local_dir"
    fi
    
    log "Syncing outputs to S3: $local_dir/ -> s3://${S3_BUCKET}/${s3_path}/"
    aws s3 sync "$local_dir" "s3://${S3_BUCKET}/${s3_path}" --delete || error "Failed to sync to S3"
}

list_cities_from_s3() {
    log "Listing DEM files from S3: s3://${S3_BUCKET}/dems/"
    
    # Use aws s3 ls to list .tif files and extract city names
    local cities=$(aws s3 ls "s3://${S3_BUCKET}/dems/" | grep "\.tif$" | awk '{print $NF}' | sed 's/\.tif$//')
    
    if [[ -z "$cities" ]]; then
        error "No DEM files found in S3 bucket under dems/"
    fi
    
    echo "$cities"
}

#####################################################################
# Main Processing Functions
#####################################################################

process_city() {
    local city=$1
    local city_work_dir="${WORK_DIR}/${city}"
    local output_dir="${city_work_dir}/outputs"
    
    log "Processing city: $city"
    log "Working directory: $city_work_dir"
    log "Output directory: $output_dir"
    
    mkdir -p "$city_work_dir"
    mkdir -p "$output_dir"
    
    # Use absolute paths - avoid cd calls
    # Download required files
    log "=== Downloading files from S3 ==="
    download_from_s3 "dems/${city}.tif" "${city_work_dir}/${city}.tif"
    download_from_s3 "overflow/${city}/${city}_burn.tif" "${city_work_dir}/${city}_burn.tif"
    download_from_s3 "overflow/${city}/dem_filled.tif" "${city_work_dir}/dem_filled.tif"
    download_from_s3 "overflow/${city}/accum.tif" "${city_work_dir}/accum.tif"
    
    # Calculate sink depths
    log "=== Calculating sink depths ==="
    if [[ -f "$output_dir/gdal-sinks.tif" ]]; then
        log "Sink depths already calculated (skipping)"
    else
        log "Running gdal_calc: dem_filled.tif - ${city}_burn.tif = gdal-sinks.tif"
        gdal_calc.py -A "${city_work_dir}/dem_filled.tif" -B "${city_work_dir}/${city}_burn.tif" \
            --outfile="$output_dir/gdal-sinks.tif" \
            --calc="A-B" \
            --co="TILED=YES" || error "Failed to calculate sink depths"
    fi
    
    # Create sinks mask
    log "=== Creating sinks mask ==="
    if [[ -f "$output_dir/gdal-sinks-mask.tif" ]]; then
        log "Sinks mask already exists (skipping)"
    else
        log "Creating binary mask where depth > 0"
        gdal_calc.py -A "$output_dir/gdal-sinks.tif" \
            --outfile="$output_dir/gdal-sinks-mask.tif" \
            --calc="A>0" \
            --co="TILED=YES" || error "Failed to create sinks mask"
    fi
    
    # Polygonize sinks mask
    log "=== Polygonizing sinks mask ==="
    if [[ -f "$output_dir/gdal-sinks-poly.gpkg" ]]; then
        log "Sinks polygons already exist (skipping)"
    else
        log "Converting raster to polygons"
        gdal_polygonize.py -mask "$output_dir/gdal-sinks-mask.tif" \
            "$output_dir/gdal-sinks-mask.tif" "$output_dir/gdal-sinks-poly.gpkg" sinks || error "Failed to polygonize"
    fi
    
    # Add region_id to polygons
    log "=== Adding region_id to polygons ==="
    if [[ -f "$output_dir/gdal-sinks-poly_region-id.gpkg" ]]; then
        log "Region IDs already assigned (skipping)"
    else
        log "Adding fid as region_id"
        ogr2ogr -nln sinks_region_id "$output_dir/gdal-sinks-poly_region-id.gpkg" "$output_dir/gdal-sinks-poly.gpkg" \
            -sql "SELECT fid, fid AS region_id, DN, geom FROM sinks" || error "Failed to add region_id"
    fi
    
    # Filter small sinks (area < 2500 ft²)
    log "=== Filtering small sinks (area < 2500 ft²) ==="
    if [[ -f "$output_dir/gdal-sinks-poly_filtered.gpkg" ]]; then
        log "Filtered polygons already exist (skipping)"
    else
        log "Filtering out sinks smaller than 2500 ft²"
        ogr2ogr -nln sinks_filtered "$output_dir/gdal-sinks-poly_filtered.gpkg" "$output_dir/gdal-sinks-poly_region-id.gpkg" \
            -sql "SELECT fid, region_id, DN, geom FROM sinks_region_id WHERE ST_Area(geom) > 2500" || error "Failed to filter"
    fi
    
    # Rasterize region_id
    log "=== Rasterizing region_id ==="
    if [[ -f "$output_dir/gdal-sinks-unique.tif" ]]; then
        log "Region ID raster already exists (skipping)"
    else
        log "Rasterizing filtered polygons to 4x4 grid"
        gdal_rasterize -a region_id -where "DN=1" \
            -tr 4 4 -tap \
            -a_nodata 0 \
            -co TILED=YES \
            "$output_dir/gdal-sinks-poly_filtered.gpkg" "$output_dir/gdal-sinks-unique.tif" || error "Failed to rasterize"
    fi
    
    # Zonal statistics on sink depths
    log "=== Computing zonal statistics on sink depths ==="
    if [[ -f "$output_dir/gdal-sinks-stats.csv" ]]; then
        log "Sink depth stats already computed (skipping)"
    else
        log "Computing count, max, sum, mean for sink depths"
        gdal_translate -ot Int32 "$output_dir/gdal-sinks.tif" "$output_dir/gdal-sinks-int32.tif" -co TILED=YES
        gdalinfo -stats "$output_dir/gdal-sinks-int32.tif" > /dev/null
        gdal raster zonal-stats --input "$output_dir/gdal-sinks-int32.tif" \
            --zones "$output_dir/gdal-sinks-unique.tif" \
            --output-format csv \
            --output "$output_dir/gdal-sinks-stats.csv" \
            --stat count --stat max --stat sum --stat mean || error "Failed to compute zonal stats"
    fi
    
    # Convert flow accumulation to Int32
    log "=== Converting flow accumulation to Int32 ==="
    if [[ -f "$output_dir/accum-int32.tif" ]]; then
        log "Flow accumulation Int32 already exists (skipping)"
    else
        log "Converting accum.tif to Int32 format"
        gdal_translate "${city_work_dir}/accum.tif" "$output_dir/accum-int32.tif" -ot Int32 -co TILED=YES || error "Failed to convert flow accumulation"
    fi
    
    # Zonal statistics on flow accumulation
    log "=== Computing zonal statistics on flow accumulation ==="
    if [[ -f "$output_dir/gdal-flowacc-stats.csv" ]]; then
        log "Flow accumulation stats already computed (skipping)"
    else
        log "Computing max flow accumulation per region"
        gdalinfo -stats "$output_dir/accum-int32.tif" > /dev/null
        gdal raster zonal-stats --input "$output_dir/accum-int32.tif" \
            --zones "$output_dir/gdal-sinks-unique.tif" \
            --output-format csv \
            --output "$output_dir/gdal-flowacc-stats.csv" \
            --stat max || error "Failed to compute flow accumulation stats"
    fi
    
    log "=== Processing complete for $city ==="
    log "Output directory: $output_dir"
    
    # Sync all output files to S3
    log "=== Uploading output files to S3 ==="
    sync_to_s3 "$output_dir" "sinks/${city}"
    log "Upload complete for $city"
    
    # Clean up local files to save space (keep empty city folder)
    log "=== Cleaning up local files ==="
    find "$city_work_dir" -mindepth 1 -delete
    log "Local cleanup complete for $city"
    
    return 0
}

#####################################################################
# Main Entry Point
#####################################################################

main() {
    if [[ $# -lt 1 ]]; then
        cat <<EOF
Usage: $0 <city_name> [<city_name2> ...]
       $0 all

Process one or more urban areas to extract and analyze sinks.

The script downloads DEM and flow data from S3, calculates sink depths,
creates sink polygons, and computes zonal statistics.

Examples:
  $0 fargo                    # Process fargo
  $0 fargo omaha minneapolis  # Process multiple cities
  $0 all                      # Process all cities in S3

Environment variables:
  WORK_DIR     Working directory (default: current directory)
  VERBOSE      Enable verbose output (0=off, 1=on; default: 0)

EOF
        exit 1
    fi
    
    check_prerequisites
    
    log "Starting Urban Areas Sink Analysis"
    log "S3 Bucket: $S3_BUCKET"
    
    local failed_cities=()
    
    # Handle "all" special case
    if [[ "$1" == "all" ]]; then
        log "Processing all cities in preferred order..."
        for city in "${CITIES_ORDERED[@]}"; do
            if ! process_city "$city"; then
                log "[WARNING] Failed to process $city, continuing to next city..."
                failed_cities+=("$city")
            fi
        done
    else
        # Process specified cities
        for city in "$@"; do
            if ! process_city "$city"; then
                log "[WARNING] Failed to process $city, continuing to next city..."
                failed_cities+=("$city")
            fi
        done
    fi
    
    log "Urban Areas Sink Analysis complete"
    
    if [[ ${#failed_cities[@]} -gt 0 ]]; then
        log "[SUMMARY] The following cities failed to process:"
        for city in "${failed_cities[@]}"; do
            log "  - $city"
        done
        log "You can retry failed cities with: $0 ${failed_cities[*]}"
        exit 1
    else
        log "All cities processed successfully"
        exit 0
    fi
}

main "$@"
