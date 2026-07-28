#!/bin/bash

#####################################################################
# Download NSI Data Script
# 
# Purpose: Download National Structure Inventory data for a city
# using the DEM extent (reprojected to WGS84) as the bounding box.
#
# The script:
# 1. Extracts the DEM extent and projection
# 2. Reprojects to WGS84
# 3. Queries the NSI API
# 4. Saves results as GeoJSON and other formats
#####################################################################

set -euo pipefail

# Configuration
S3_BUCKET="arc-urban-areas-894169284666-us-west-2-an"
NSI_API_BASE="https://nsi.sec.usace.army.mil/nsiapi/structures"
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
    check_command "gdalinfo"
    check_command "gdaltransform"
    check_command "ogr2ogr"
    check_command "curl"
    check_command "jq"
    log "All prerequisites found"
}

#####################################################################
# DEM Extent and Projection Functions
#####################################################################

get_dem_info() {
    local dem_file=$1
    
    debug "Getting DEM info from: $dem_file"
    gdalinfo "$dem_file"
}

extract_projection() {
    local gdalinfo_output=$1
    
    # Extract the full WKT projection string
    # It starts after "Coordinate System is:" and ends at blank line
    local wkt=$(echo "$gdalinfo_output" | sed -n '/^Coordinate System is:/,/^$/p' | sed '1d;$d')
    
    if [[ -z "$wkt" ]]; then
        error "Could not extract projection from DEM metadata"
    fi
    
    echo "$wkt"
}

extract_extent() {
    local gdalinfo_output=$1
    
    # Extract corner coordinates
    local upper_left=$(echo "$gdalinfo_output" | grep "Upper Left" | grep -oP '\(([^)]+)\)' | head -1 | tr -d '()')
    local lower_right=$(echo "$gdalinfo_output" | grep "Lower Right" | grep -oP '\(([^)]+)\)' | head -1 | tr -d '()')
    
    if [[ -z "$upper_left" ]] || [[ -z "$lower_right" ]]; then
        error "Could not extract extent from DEM"
    fi
    
    echo "$upper_left $lower_right"
}

parse_coordinates() {
    local coords=$1
    
    # coords format: "x1, y1 x2, y2"
    # Use awk to split by spaces/commas more reliably
    # Extract all numeric values
    local values=($(echo "$coords" | tr ',[:space:]' '\n' | grep -E '^-?[0-9]+(\.[0-9]+)?$'))
    
    if [[ ${#values[@]} -lt 4 ]]; then
        error "Could not parse 4 coordinates from: $coords"
    fi
    
    local ul_x=${values[0]}
    local ul_y=${values[1]}
    local lr_x=${values[2]}
    local lr_y=${values[3]}
    
    echo "$ul_x $ul_y $lr_x $lr_y"
}

reproject_extent_to_wgs84() {
    local source_wkt=$1
    local ul_x=$2
    local ul_y=$3
    local lr_x=$4
    local lr_y=$5
    
    log "Reprojecting extent to WGS84 (EPSG:4326)"
    
    # Create temporary file for coordinates
    local temp_coords=$(mktemp)
    
    debug "Input coordinates file:"
    debug "UL: $ul_x, $ul_y"
    debug "LR: $lr_x, $lr_y"
    
    cat > "$temp_coords" << EOF
$ul_x $ul_y
$lr_x $lr_y
EOF
    
    # Reproject using gdaltransform - capture all output including errors
    local wgs84_coords
    local gdal_error
    wgs84_coords=$(gdaltransform -s_srs "$source_wkt" -t_srs "EPSG:4326" < "$temp_coords" 2>&1)
    local gdal_exit=$?
    
    debug "gdaltransform exit code: $gdal_exit"
    debug "Raw output: '$wgs84_coords'"
    
    rm "$temp_coords"
    
    if [[ $gdal_exit -ne 0 ]] || [[ -z "$wgs84_coords" ]]; then
        error "gdaltransform failed with exit code $gdal_exit: $wgs84_coords"
    fi
    
    echo "$wgs84_coords"
}

format_wgs84_bbox() {
    local ul_lon=$1
    local ul_lat=$2
    local lr_lon=$3
    local lr_lat=$4
    
    # NSI API expects: lon1,lat1,lon2,lat2,lon3,lat3,lon4,lat4,lon5,lat5
    # (a polygon with 5 points, first = last)
    # Format as a bounding box polygon
    local bbox="${ul_lon},${ul_lat},${lr_lon},${ul_lat},${lr_lon},${lr_lat},${ul_lon},${lr_lat},${ul_lon},${ul_lat}"
    
    echo "$bbox"
}

#####################################################################
# NSI API Functions
#####################################################################

create_tile_bboxes() {
    local ul_lon=$1
    local ul_lat=$2
    local lr_lon=$3
    local lr_lat=$4
    local tile_size=${5:-0.5}  # Default to 0.5 degrees
    
    # Ensure coordinates are in correct order (lon,lat)
    local min_lon=$(echo "$ul_lon $lr_lon" | awk '{print ($1 < $2) ? $1 : $2}')
    local max_lon=$(echo "$ul_lon $lr_lon" | awk '{print ($1 > $2) ? $1 : $2}')
    local min_lat=$(echo "$ul_lat $lr_lat" | awk '{print ($1 < $2) ? $1 : $2}')
    local max_lat=$(echo "$ul_lat $lr_lat" | awk '{print ($1 > $2) ? $1 : $2}')
    
    debug "Tile extent: lon[$min_lon, $max_lon], lat[$min_lat, $max_lat]"
    
    # Create tiles
    local tiles=()
    local current_lon=$min_lon
    
    while (( $(echo "$current_lon < $max_lon" | bc -l) )); do
        local next_lon=$(echo "$current_lon + $tile_size" | bc -l)
        # Clamp to max
        if (( $(echo "$next_lon > $max_lon" | bc -l) )); then
            next_lon=$max_lon
        fi
        
        local current_lat=$min_lat
        while (( $(echo "$current_lat < $max_lat" | bc -l) )); do
            local next_lat=$(echo "$current_lat + $tile_size" | bc -l)
            # Clamp to max
            if (( $(echo "$next_lat > $max_lat" | bc -l) )); then
                next_lat=$max_lat
            fi
            
            # Format as NSI API bbox: lon1,lat1,lon2,lat2,lon3,lat3,lon4,lat4,lon1,lat1
            local tile_bbox="${current_lon},${current_lat},${next_lon},${current_lat},${next_lon},${next_lat},${current_lon},${next_lat},${current_lon},${current_lat}"
            tiles+=("$tile_bbox")
            
            current_lat=$next_lat
        done
        
        current_lon=$next_lon
    done
    
    # Output tiles (one per line)
    printf '%s\n' "${tiles[@]}"
}

query_nsi_api_tile() {
    local bbox=$1
    local output_file=$2
    local tile_num=$3
    local total_tiles=$4
    
    log "[$tile_num/$total_tiles] Querying NSI API with bbox: $bbox"
    
    local url="${NSI_API_BASE}?bbox=${bbox}"
    debug "API URL: $url"
    
    if ! curl -s "$url" -o "$output_file"; then
        error "Failed to download from NSI API for tile $tile_num"
    fi
    
    # Validate JSON response
    if ! jq empty "$output_file" 2>/dev/null; then
        error "Invalid JSON response from NSI API for tile $tile_num"
    fi
    
    # Check for errors in response
    if jq -e '.error' "$output_file" 2>/dev/null; then
        local error_msg=$(jq -r '.error' "$output_file")
        warn "NSI API returned error for tile $tile_num: $error_msg"
        return 1
    fi
    
    local feature_count=$(jq '.features | length' "$output_file")
    log "  -> Tile $tile_num: $feature_count structures"
    return 0
}

merge_nsi_tiles_to_gpkg() {
    local output_gpkg=$1
    shift
    local tile_files=("$@")
    
    log "Merging ${#tile_files[@]} GeoJSON tiles to GeoPackage..."
    
    # Remove existing output if present
    rm -f "$output_gpkg"
    
    # Append each tile to the GeoPackage
    local tile_count=0
    for tile_file in "${tile_files[@]}"; do
        if [[ ! -f "$tile_file" ]]; then
            continue
        fi
        
        # Check if file has content
        local feature_count=$(jq '.features | length' "$tile_file" 2>/dev/null || echo 0)
        if [[ "$feature_count" -eq 0 ]]; then
            debug "Skipping empty tile: $tile_file"
            continue
        fi
        
        tile_count=$((tile_count + 1))
        
        if [[ ! -f "$output_gpkg" ]]; then
            # First tile - create new GeoPackage
            log "  -> Creating GeoPackage with tile 1"
            ogr2ogr -f GPKG "$output_gpkg" "$tile_file" -nln nsi_structures 2>&1 | grep -v "Warning" || true
        else
            # Subsequent tiles - append to existing GeoPackage
            log "  -> Appending tile $tile_count ($feature_count features)"
            ogr2ogr -f GPKG -append "$output_gpkg" "$tile_file" -nln nsi_structures 2>&1 | grep -v "Warning" || true
        fi
    done
    
    if [[ ! -f "$output_gpkg" ]]; then
        log "[ERROR] Failed to create GeoPackage - no valid tiles found"
        return 1
    fi
    
    local total_count=$(ogrinfo -sql "SELECT COUNT(*) as count FROM nsi_structures" "$output_gpkg" 2>/dev/null | grep count | awk '{print $NF}' || echo "?")
    log "Merged total: $total_count structures"
    return 0
}

query_nsi_api() {
    local ul_lon=$1
    local ul_lat=$2
    local lr_lon=$3
    local lr_lat=$4
    local output_file=$5
    local tile_size=${6:-0.5}  # Default to 0.5 degrees
    
    log "Creating 0.5-degree tiles for NSI download..."
    
    # Create tile bboxes
    local tiles=($(create_tile_bboxes "$ul_lon" "$ul_lat" "$lr_lon" "$lr_lat" "$tile_size"))
    local total_tiles=${#tiles[@]}
    
    log "Created $total_tiles tiles"
    
    # Check if we have any tiles
    if [[ $total_tiles -eq 0 ]]; then
        log "[ERROR] Failed to create tiles from bounding box"
        return 1
    fi
    
    # Create temporary directory for tile results
    local temp_dir=$(mktemp -d)
    local tile_files=()
    
    # Query each tile
    for i in "${!tiles[@]}"; do
        local tile_num=$((i + 1))
        local tile_bbox="${tiles[$i]}"
        local tile_file="${temp_dir}/tile_${tile_num}.geojson"
        
        query_nsi_api_tile "$tile_bbox" "$tile_file" "$tile_num" "$total_tiles" || true
        tile_files+=("$tile_file")
    done
    
    # Merge all tiles into GeoPackage
    if ! merge_nsi_tiles_to_gpkg "$output_file" "${tile_files[@]}"; then
        log "[ERROR] Failed to merge NSI tiles"
        rm -rf "$temp_dir"
        return 1
    fi
    
    # Clean up
    rm -rf "$temp_dir"
    return 0
}

#####################################################################
# S3 Operations
#####################################################################

list_cities_from_s3() {
    log "Listing DEM files from S3: s3://${S3_BUCKET}/dems/"
    
    # Use aws s3 ls to list .tif files and extract city names
    local cities=$(aws s3 ls "s3://${S3_BUCKET}/dems/" | grep "\.tif$" | awk '{print $NF}' | sed 's/\.tif$//')
    
    if [[ -z "$cities" ]]; then
        error "No DEM files found in S3 bucket under dems/"
    fi
    
    echo "$cities"
}

sync_to_s3() {
    local local_dir=$1
    local s3_path=$2
    
    if [[ ! -d "$local_dir" ]]; then
        error "Directory not found: $local_dir"
    fi
    
    log "Uploading NSI data to S3: $local_dir/ -> s3://${S3_BUCKET}/${s3_path}/"
    aws s3 sync "$local_dir" "s3://${S3_BUCKET}/${s3_path}" --delete || error "Failed to sync to S3"
}

download_nsi_for_city() {
    local city=$1
    local city_work_dir="${WORK_DIR}/${city}"
    local output_dir="${city_work_dir}/nsi"
    
    # DEM paths
    local dem_file="${city_work_dir}/${city}.tif"
    local dem_remote="/vsis3/${S3_BUCKET}/dems/${city}.tif"
    local dem_s3="s3://${S3_BUCKET}/dems/${city}.tif"
    
    log "Downloading NSI data for city: $city"
    
    # Create output directory
    mkdir -p "$output_dir"
    
    # For metadata extraction, we need a local file
    # Check if local DEM exists, otherwise download temporarily
    local dem_path
    
    if [[ -f "$dem_file" ]]; then
        dem_path="$dem_file"
        log "Using local DEM: $dem_path"
    else
        # Download DEM for metadata
        dem_path="$dem_file"
        log "Downloading DEM for metadata: $dem_s3"
        if ! aws s3 cp "$dem_s3" "$dem_path" --quiet; then
            error "Failed to download DEM from S3: $dem_s3"
        fi
        log "Downloaded DEM"
    fi
    
    # Get DEM information
    log "=== Extracting DEM extent and projection ==="
    local gdalinfo_output=$(get_dem_info "$dem_path")
    
    local source_wkt=$(extract_projection "$gdalinfo_output")
    log "Custom projection detected (Albers Equal Area)"
    debug "Full WKT: $source_wkt"
    
    local extent_str=$(extract_extent "$gdalinfo_output")
    debug "DEM extent (source projection): $extent_str"
    
    local coords=$(parse_coordinates "$extent_str")
    local ul_x=$(echo "$coords" | cut -d' ' -f1)
    local ul_y=$(echo "$coords" | cut -d' ' -f2)
    local lr_x=$(echo "$coords" | cut -d' ' -f3)
    local lr_y=$(echo "$coords" | cut -d' ' -f4)
    
    debug "Extent coordinates: UL=($ul_x, $ul_y) LR=($lr_x, $lr_y)"
    
    # Validate that we got valid coordinates
    if [[ -z "$ul_x" || -z "$ul_y" || -z "$lr_x" || -z "$lr_y" ]]; then
        log "[ERROR] Failed to extract valid coordinates from DEM"
        return 1
    fi
    
    # Reproject to WGS84
    log "=== Reprojecting extent to WGS84 ==="
    local wgs84_coords=$(reproject_extent_to_wgs84 "$source_wkt" "$ul_x" "$ul_y" "$lr_x" "$lr_y")
    
    local ul_lon=$(echo "$wgs84_coords" | head -1 | awk '{print $1}')
    local ul_lat=$(echo "$wgs84_coords" | head -1 | awk '{print $2}')
    local lr_lon=$(echo "$wgs84_coords" | tail -1 | awk '{print $1}')
    local lr_lat=$(echo "$wgs84_coords" | tail -1 | awk '{print $2}')
    
    # Validate reprojected coordinates
    if [[ -z "$ul_lon" || -z "$ul_lat" || -z "$lr_lon" || -z "$lr_lat" ]]; then
        log "[ERROR] Failed to reproject coordinates to WGS84"
        return 1
    fi
    
    log "WGS84 extent:"
    log "  Upper Left: $ul_lon, $ul_lat"
    log "  Lower Right: $lr_lon, $lr_lat"
    
    # Query NSI API with tiling (output directly to GeoPackage)
    log "=== Querying NSI API (with 0.5-degree tiling) ==="
    local nsi_gpkg="${output_dir}/nsi_structures.gpkg"
    if ! query_nsi_api "$ul_lon" "$ul_lat" "$lr_lon" "$lr_lat" "$nsi_gpkg"; then
        log "[ERROR] Failed to query NSI API"
        return 1
    fi
    
    # Convert to other formats
    log "=== Converting to additional formats ==="
    
    # GeoJSON (optional, from GPKG)
    # local nsi_geojson="${output_dir}/nsi_structures.geojson"
    # if [[ ! -f "$nsi_geojson" ]]; then
    #     log "Converting GeoPackage to GeoJSON"
    #     ogr2ogr -f "GeoJSON" "$nsi_geojson" "$nsi_gpkg" 2>&1 | grep -v "Warning" || true
    # fi
    
    # Shapefile
    local nsi_shp_dir="${output_dir}/nsi_structures"
    if [[ ! -d "$nsi_shp_dir" ]]; then
        log "Converting GeoJSON to Shapefile"
        mkdir -p "$nsi_shp_dir"
        ogr2ogr -f "ESRI Shapefile" "$nsi_shp_dir/" "$nsi_geojson" 2>&1 | grep -v "Warning" || true
    fi
    
    log "=== NSI download complete for $city ==="
    log "Output files:"
    log "  - nsi_structures.gpkg (GeoPackage format)"
    log "  - nsi_structures.geojson (GeoJSON format)"
    log "  - nsi_structures/ (Shapefile directory)"
    
    # Upload results to S3
    log "=== Uploading NSI data to S3 ==="
    sync_to_s3 "$output_dir" "nsi/${city}"
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

Download NSI (National Structure Inventory) data for one or more cities
using their DEM extents (reprojected to WGS84) as bounding boxes.

The script:
1. Extracts the DEM extent and projection
2. Reprojects to WGS84
3. Queries the NSI API
4. Saves results as GeoJSON, GeoPackage, and Shapefile

Examples:
  $0 fargo                    # Download NSI for fargo
  $0 fargo omaha minneapolis  # Download NSI for multiple cities
  $0 all                      # Download NSI for all cities in S3

Environment variables:
  WORK_DIR     Working directory (default: current directory)
  VERBOSE      Enable verbose output (0=off, 1=on; default: 0)

EOF
        exit 1
    fi
    
    check_prerequisites
    
    log "Starting NSI Data Download"
    
    local failed_cities=()
    
    # Handle "all" special case
    if [[ "$1" == "all" ]]; then
        log "Processing all cities in preferred order..."
        for city in "${CITIES_ORDERED[@]}"; do
            if ! download_nsi_for_city "$city"; then
                log "[WARNING] Failed to process $city, continuing to next city..."
                failed_cities+=("$city")
            fi
        done
    else
        # Process specified cities
        for city in "$@"; do
            if ! download_nsi_for_city "$city"; then
                log "[WARNING] Failed to process $city, continuing to next city..."
                failed_cities+=("$city")
            fi
        done
    fi
    
    log "NSI Data Download complete"
    
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
