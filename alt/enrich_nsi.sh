#!/bin/bash

#####################################################################
# Enrich NSI Structures with DEM, Sink, and Flow Statistics
#
# Purpose: Post-process NSI building points with terrain and sink
# analysis attributes from DEM and sink-processing workflows.
#
# Steps:
# 1. Extract DEM elevations at NSI building locations
# 2. Join sink and flow statistics to sink polygons
# 3. Rasterize region_id, sample it at NSI points, join stats by region_id
# 4. Join building class information to NSI structures
#####################################################################

set -euo pipefail

WORK_DIR="${WORK_DIR:-.}"
VERBOSE="${VERBOSE:=0}"

# S3 Configuration
S3_BUCKET="arc-urban-areas-894169284666-us-west-2-an"

# Rasterization resolution (meters) for the region_id raster.
# Should match the resolution of the source sink/flow rasters.
RASTER_RES="${RASTER_RES:-4}"

# Arrays to store presigned links for summary at end
declare -a PRESIGNED_CITIES
declare -a PRESIGNED_FILES
declare -a PRESIGNED_URLS

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

check_file_exists() {
    local file=$1
    local desc=$2
    if [[ ! -f "$file" ]]; then
        error "$desc not found: $file"
    fi
}

check_prerequisites() {
    log "Checking prerequisites..."
    check_command "ogrinfo"
    check_command "ogr2ogr"
    check_command "gdallocationinfo"
    check_command "gdal_translate"
    check_command "gdal_rasterize"
    check_command "sqlite3"
    check_command "python3"
    check_command "aws"
    log "All prerequisites found"
}

#####################################################################
# S3 Operations
#####################################################################

download_from_s3() {
    local s3_path=$1
    local local_path=$2

    if [[ -f "$local_path" ]]; then
        debug "File already exists: $local_path (skipping download)"
        return 0
    fi

    log "Downloading from S3: $s3_path -> $local_path"
    aws s3 cp "s3://${S3_BUCKET}/${s3_path}" "$local_path" --quiet || error "Failed to download $s3_path"
}

sync_to_s3() {
    local local_dir=$1
    local s3_path=$2

    if [[ ! -d "$local_dir" ]]; then
        error "Directory not found: $local_dir"
    fi

    log "Uploading enriched NSI data to S3: $local_dir/ -> s3://${S3_BUCKET}/${s3_path}/"
    aws s3 sync "$local_dir" "s3://${S3_BUCKET}/${s3_path}" --delete || error "Failed to sync to S3"
}

generate_presigned_links() {
    local city=$1
    local output_dir=$2
    local s3_path="enriched_nsi/${city}"
    
    # Max expiration time in seconds (604800 = 7 days, AWS maximum)
    local expiration=604800
    
    # Array of output files
    local files=(
        "gdal-sinks-poly_filtered_enriched.gpkg"
        "nsi_structures_enriched_with_class.gpkg"
    )
    
    # Generate presigned URLs and store in global arrays
    for file in "${files[@]}"; do
        local s3_uri="s3://${S3_BUCKET}/${s3_path}/${file}"
        
        # Check if file exists in output directory
        if [[ ! -f "${output_dir}/${file}" ]]; then
            continue
        fi
        
        # Generate presigned URL
        local presigned_url
        if presigned_url=$(aws s3 presign "$s3_uri" --expires-in $expiration 2>/dev/null); then
            # Store in global arrays for summary
            PRESIGNED_CITIES+=("$city")
            PRESIGNED_FILES+=("$file")
            PRESIGNED_URLS+=("$presigned_url")
        fi
    done
}

print_presigned_summary() {
    if [[ ${#PRESIGNED_URLS[@]} -eq 0 ]]; then
        return 0
    fi
    
    log ""
    log "╔════════════════════════════════════════════════════════════════╗"
    log "║          PRESIGNED DOWNLOAD LINKS (7-day expiration)          ║"
    log "╚════════════════════════════════════════════════════════════════╝"
    log ""
    
    # Group by city
    local current_city=""
    for i in "${!PRESIGNED_CITIES[@]}"; do
        local city="${PRESIGNED_CITIES[$i]}"
        local file="${PRESIGNED_FILES[$i]}"
        local url="${PRESIGNED_URLS[$i]}"
        
        # Print city header on first occurrence
        if [[ "$city" != "$current_city" ]]; then
            if [[ -n "$current_city" ]]; then
                log ""
            fi
            log "${GREEN}City: ${city}${NC}"
            current_city="$city"
        fi
        
        # Shorten filename for readability
        local short_file="${file%.gpkg}"
        log "  • ${short_file}"
        log "    ${url}"
    done
    
    log ""
    log "All links valid for 7 days"
    log ""
}


count_features() {
    local file=$1
    ogrinfo -sql "SELECT COUNT(*) as count FROM $(basename "$file" .gpkg)" "$file" 2>/dev/null | grep count | awk '{print $NF}' || echo "?"
}

#####################################################################
# Step 1: Extract DEM Elevations at NSI Building Locations
#####################################################################

extract_dem_elevations() {
    local city=$1
    local city_work_dir="${WORK_DIR}/${city}"
    local output_dir="${city_work_dir}/outputs"

    log "=== Step 1: Extracting DEM elevations at NSI locations ==="

    local dem_file="${city_work_dir}/${city}.tif"
    local filled_dem="${city_work_dir}/dem_filled.tif"
    local nsi_gpkg="${city_work_dir}/nsi_structures.gpkg"
    local nsi_elev="${output_dir}/nsi_structures_elev.gpkg"

    # Download required files from S3 if not present
    mkdir -p "$output_dir"

    if [[ ! -f "$dem_file" ]]; then
        log "Downloading original DEM from S3..."
        download_from_s3 "dems/${city}.tif" "$dem_file"
    fi

    if [[ ! -f "$filled_dem" ]]; then
        log "Downloading filled DEM from S3..."
        download_from_s3 "overflow/${city}/dem_filled.tif" "$filled_dem"
    fi

    if [[ ! -f "$nsi_gpkg" ]]; then
        log "Downloading NSI structures from S3..."
        download_from_s3 "nsi/${city}/nsi_structures.gpkg" "$nsi_gpkg"
    fi

    # Check inputs
    check_file_exists "$dem_file" "Original DEM"
    check_file_exists "$filled_dem" "Filled DEM"
    check_file_exists "$nsi_gpkg" "NSI structures GeoPackage"

    if [[ -f "$nsi_elev" ]]; then
        log "NSI elevations already extracted (skipping)"
        return 0
    fi

    log "Extracting elevations from original and filled DEMs..."

    # Copy NSI GeoPackage as base for enriched version
    cp "$nsi_gpkg" "$nsi_elev"

    # Add elevation fields using SQLite directly (bypasses layer name issues)
    log "Adding elevation fields to NSI structures..."
    sqlite3 "$nsi_elev" << 'SQL_EOF'
ALTER TABLE nsi_structures ADD COLUMN dem_elev REAL;
ALTER TABLE nsi_structures ADD COLUMN filled_elev REAL;
SQL_EOF

    # Extract elevation using Python script (more reliable than direct OGR)
    local python_script
    python_script=$(mktemp --suffix=.py)
    cat > "$python_script" << 'PYTHON_EOF'
import sys
from osgeo import gdal, ogr, osr

dem_path = sys.argv[1]
filled_dem_path = sys.argv[2]
gpkg_path = sys.argv[3]

gdal.UseExceptions()
ogr.UseExceptions()

# Open DEMs
dem = gdal.Open(dem_path)
filled_dem = gdal.Open(filled_dem_path)

if not dem or not filled_dem:
    print("ERROR: Could not open DEM files", file=sys.stderr)
    sys.exit(1)

# Get DEM info
dem_band = dem.GetRasterBand(1)
dem_gt = dem.GetGeoTransform()
dem_srs_wkt = dem.GetProjection()
dem_srs = osr.SpatialReference(wkt=dem_srs_wkt)

filled_band = filled_dem.GetRasterBand(1)

# Open GeoPackage
ds = ogr.Open(gpkg_path, 1)  # 1 = read-write

if not ds:
    print("ERROR: Could not open GeoPackage", file=sys.stderr)
    sys.exit(1)

# Get first layer (should be nsi_structures)
if ds.GetLayerCount() == 0:
    print(f"ERROR: No layers found in {gpkg_path}", file=sys.stderr)
    sys.exit(1)

layer = ds.GetLayer(0)
if not layer:
    print(f"ERROR: Could not get layer", file=sys.stderr)
    sys.exit(1)

# Get the layer's spatial reference
layer_srs = layer.GetSpatialRef()

# Create coordinate transformation if needed
coordTrans = None
if layer_srs and not layer_srs.IsSame(dem_srs):
    coordTrans = osr.CoordinateTransformation(layer_srs, dem_srs)

# Process each feature
count = 0
updated = 0
for feat in layer:
    geom = feat.GetGeometryRef()
    if not geom:
        count += 1
        continue

    # Get point coordinates (assuming point geometry)
    if geom.GetGeometryType() == 1:  # wkbPoint
        x = geom.GetX()
        y = geom.GetY()

        # Transform coordinates to DEM projection if needed
        if coordTrans:
            try:
                x, y, _ = coordTrans.TransformPoint(x, y, 0)
            except Exception as e:
                print(f"Transformation error for point ({geom.GetX()}, {geom.GetY()}): {e}", file=sys.stderr)
                count += 1
                continue

        # Convert world coordinates to pixel
        px = int((x - dem_gt[0]) / dem_gt[1])
        py = int((y - dem_gt[3]) / dem_gt[5])

        # Check bounds
        if 0 <= px < dem.RasterXSize and 0 <= py < dem.RasterYSize:
            dem_val = dem_band.ReadAsArray(px, py, 1, 1)[0, 0]
            filled_val = filled_band.ReadAsArray(px, py, 1, 1)[0, 0]

            if dem_val != dem_band.GetNoDataValue() and filled_val != filled_band.GetNoDataValue():
                feat.SetField("dem_elev", float(dem_val))
                feat.SetField("filled_elev", float(filled_val))
                layer.SetFeature(feat)
                updated += 1

    count += 1
    if count % 1000 == 0:
        print(f"Processed {count} features...", file=sys.stderr)

print(f"Updated {updated}/{count} features with elevation values", file=sys.stderr)
ds = None
dem = None
filled_dem = None
sys.exit(0)
PYTHON_EOF

    log "Running elevation extraction..."
    if ! python3 "$python_script" "$dem_file" "$filled_dem" "$nsi_elev" 2>&1; then
        warn "Python elevation extraction failed"
        rm "$python_script"
        return 1
    else
        rm "$python_script"
    fi

    local feat_count
    feat_count=$(count_features "$nsi_elev")
    log "NSI structures with elevations: $feat_count features"
}

#####################################################################
# Step 2: Join Sink and Flow Statistics to Sink Polygons
#####################################################################

join_statistics_to_sinks() {
    local city=$1
    local city_work_dir="${WORK_DIR}/${city}"
    local output_dir="${city_work_dir}/outputs"

    log "=== Step 2: Joining statistics to sink polygons ==="

    local sink_stats="${output_dir}/gdal-sinks-stats.csv"
    local flow_stats="${output_dir}/gdal-flowacc-stats.csv"
    local sink_poly="${output_dir}/gdal-sinks-poly_filtered.gpkg"
    local sink_enriched="${output_dir}/gdal-sinks-poly_filtered_enriched.gpkg"

    # Download required files from S3 if not present
    if [[ ! -f "$sink_stats" ]]; then
        log "Downloading sink statistics from S3..."
        download_from_s3 "sinks/${city}/gdal-sinks-stats.csv" "$sink_stats"
    fi

    if [[ ! -f "$flow_stats" ]]; then
        log "Downloading flow statistics from S3..."
        download_from_s3 "sinks/${city}/gdal-flowacc-stats.csv" "$flow_stats"
    fi

    if [[ ! -f "$sink_poly" ]]; then
        log "Downloading sink polygons from S3..."
        download_from_s3 "sinks/${city}/gdal-sinks-poly_filtered.gpkg" "$sink_poly"
    fi

    check_file_exists "$sink_stats" "Sink statistics CSV"
    check_file_exists "$flow_stats" "Flow statistics CSV"
    check_file_exists "$sink_poly" "Sink polygons GeoPackage"

    if [[ -f "$sink_enriched" ]]; then
        log "Sink polygons already enriched (skipping)"
        return 0
    fi

    log "Preparing statistics tables..."

    local temp_dir
    temp_dir=$(mktemp -d)

    local temp_gpkg="${temp_dir}/stats_temp.gpkg"

    rm -f "$temp_gpkg" "$temp_gpkg-wal" "$temp_gpkg-shm"

    # Copy sink polygons to temp GeoPackage with known layer name
    ogr2ogr -f GPKG "$temp_gpkg" "$sink_poly" \
        -nln sink_poly \
        -lco SPATIAL_INDEX=YES || {
        log "[ERROR] Failed to copy sink polygon to temp GeoPackage"
        rm -rf "$temp_dir"
        return 1
    }

    # SQLite tuning before bulk import / typed table creation
    sqlite3 "$temp_gpkg" <<'SQL_EOF'
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -1000000;
PRAGMA synchronous = OFF;
PRAGMA journal_mode = MEMORY;
SQL_EOF

    # Import CSVs as raw tables
    log "Importing sink statistics CSV..."
    ogr2ogr -f GPKG -append "$temp_gpkg" "$sink_stats" \
        -nln sink_stats_raw || {
        log "[ERROR] Failed to import sink statistics CSV"
        rm -rf "$temp_dir"
        return 1
    }

    log "Importing flow statistics CSV..."
    ogr2ogr -f GPKG -append "$temp_gpkg" "$flow_stats" \
        -nln flow_stats_raw || {
        log "[ERROR] Failed to import flow statistics CSV"
        rm -rf "$temp_dir"
        return 1
    }

    # Convert raw imported CSV tables into typed, indexed lookup tables.
    # Important: this avoids CAST() in the join predicate.
    log "Creating typed statistics tables and indexes..."
sqlite3 "$temp_gpkg" <<'SQL_EOF'
DROP TABLE IF EXISTS sink_stats;
DROP TABLE IF EXISTS flow_stats;

CREATE TABLE sink_stats (
    value INTEGER PRIMARY KEY,
    sink_count INTEGER,
    sink_max REAL,
    sink_sum REAL,
    sink_mean REAL
);

INSERT INTO sink_stats (
    value,
    sink_count,
    sink_max,
    sink_sum,
    sink_mean
)
SELECT
    CAST("value" AS INTEGER),
    CAST("count" AS INTEGER),
    CAST("max" AS REAL),
    CAST("sum" AS REAL),
    CAST("mean" AS REAL)
FROM sink_stats_raw;

CREATE TABLE flow_stats (
    value INTEGER PRIMARY KEY,
    flow_max INTEGER
);

INSERT INTO flow_stats (
    value,
    flow_max
)
SELECT
    CAST("value" AS INTEGER),
    CAST("max" AS INTEGER)
FROM flow_stats_raw;

CREATE INDEX IF NOT EXISTS idx_sink_poly_region_id
    ON sink_poly(region_id);

CREATE INDEX IF NOT EXISTS idx_sink_stats_value
    ON sink_stats(value);

CREATE INDEX IF NOT EXISTS idx_flow_stats_value
    ON flow_stats(value);

ANALYZE;
SQL_EOF

    if [[ "${VERBOSE:-0}" -eq 1 ]]; then
        log "=== Diagnostic: typed table schemas and query plan ==="

        log "sink_poly schema:"
        sqlite3 "$temp_gpkg" "PRAGMA table_info(sink_poly);" | sed 's/^/  /'

        log "sink_stats schema:"
        sqlite3 "$temp_gpkg" "PRAGMA table_info(sink_stats);" | sed 's/^/  /'

        log "flow_stats schema:"
        sqlite3 "$temp_gpkg" "PRAGMA table_info(flow_stats);" | sed 's/^/  /'

        log "Row counts:"
        sqlite3 "$temp_gpkg" "SELECT 'sink_poly: ' || COUNT(*) FROM sink_poly;" | sed 's/^/  /'
        sqlite3 "$temp_gpkg" "SELECT 'sink_stats: ' || COUNT(*) FROM sink_stats;" | sed 's/^/  /'
        sqlite3 "$temp_gpkg" "SELECT 'flow_stats: ' || COUNT(*) FROM flow_stats;" | sed 's/^/  /'

        log "Query plan for typed join:"
        sqlite3 "$temp_gpkg" "
EXPLAIN QUERY PLAN
SELECT
    p.region_id,
    ss.sink_count,
    fs.flow_max
FROM sink_poly p
LEFT JOIN sink_stats ss
    ON p.region_id = ss.value
LEFT JOIN flow_stats fs
    ON p.region_id = fs.value
LIMIT 1;
" | sed 's/^/  /'
    fi

    log "Joining statistics to sink polygons..."

    local ogr_output
    if ! ogr_output=$(ogr2ogr -f GPKG "$sink_enriched" "$temp_gpkg" \
        -dialect SQLite \
        -sql "
            SELECT
                p.*,
                ss.sink_count,
                ss.sink_max,
                ss.sink_sum,
                ss.sink_mean,
                fs.flow_max
            FROM sink_poly p
            LEFT JOIN sink_stats ss
                ON p.region_id = ss.value
            LEFT JOIN flow_stats fs
                ON p.region_id = fs.value
        " \
        -nln sinks_filtered_enriched \
        -lco SPATIAL_INDEX=YES \
        2>&1); then
        log "[ERROR] Failed to join statistics: $ogr_output"
        rm -rf "$temp_dir"
        return 1
    fi

    rm -rf "$temp_dir"

    local feat_count
    feat_count=$(count_features "$sink_enriched")

    log "Enriched sink polygons: $feat_count features"
    return 0
}

#####################################################################
# Step 3: Sample Sink Attributes to NSI Structures (raster path)
#
# Replaces the polygon point-in-polygon spatial join with a much
# faster raster sampling approach:
#   1. Sample the region_id raster at each NSI point.
#   2. Join stats by region_id (looked up from the enriched polygons).
#####################################################################

sample_sink_stats_to_nsi() {
    local city=$1
    local city_work_dir="${WORK_DIR}/${city}"
    local output_dir="${city_work_dir}/outputs"

    log "=== Step 3: Sampling sink attributes to NSI structures (raster) ==="

    local nsi_elev="${output_dir}/nsi_structures_elev.gpkg"
    local sink_poly="${output_dir}/gdal-sinks-poly_filtered.gpkg"
    local sink_enriched="${output_dir}/gdal-sinks-poly_filtered_enriched.gpkg"
    local region_raster="${city_work_dir}/gdal-sinks-unique.tif"
    local nsi_final="${output_dir}/nsi_structures_enriched.gpkg"

    check_file_exists "$nsi_elev" "NSI structures with elevations"
    check_file_exists "$sink_enriched" "Enriched sink polygons"

    # Region raster is needed for sampling; download if missing
    if [[ ! -f "$region_raster" ]]; then
        log "Downloading region_id raster from S3..."
        download_from_s3 "sinks/${city}/gdal-sinks-unique.tif" "$region_raster"
    fi

    if [[ -f "$nsi_final" ]]; then
        log "Final enriched NSI structures already exist (skipping)"
        return 0
    fi

    #################################################################
    # 3a. Copy NSI points and add attribute columns
    #################################################################
    cp "$nsi_elev" "$nsi_final"

    sqlite3 "$nsi_final" << 'SQL_EOF'
ALTER TABLE nsi_structures ADD COLUMN region_id INTEGER;
ALTER TABLE nsi_structures ADD COLUMN sink_count INTEGER;
ALTER TABLE nsi_structures ADD COLUMN sink_max REAL;
ALTER TABLE nsi_structures ADD COLUMN sink_sum REAL;
ALTER TABLE nsi_structures ADD COLUMN sink_mean REAL;
ALTER TABLE nsi_structures ADD COLUMN flow_max INTEGER;
SQL_EOF

    #################################################################
    # 3b. Sample region_id at NSI points and join stats by region_id
    #################################################################
    log "Sampling region_id raster at NSI points and joining stats..."

    local python_script
    python_script=$(mktemp --suffix=.py)
    cat > "$python_script" << 'PYTHON_EOF'
import sys
from osgeo import gdal, ogr, osr

region_path = sys.argv[1]     # region_id raster
enriched_path = sys.argv[2]   # enriched sink polygons (region_id -> stats)
gpkg_path = sys.argv[3]       # NSI points (read-write)

gdal.UseExceptions()
ogr.UseExceptions()

# --- Build region_id -> stats lookup from the enriched polygons ---
eds = ogr.Open(enriched_path)
if not eds:
    print("ERROR: Could not open enriched sink polygons", file=sys.stderr)
    sys.exit(1)
elayer = eds.GetLayer(0)

stats = {}
for feat in elayer:
    rid = feat.GetField("region_id")
    if rid is None:
        continue
    stats[int(rid)] = (
        feat.GetField("sink_count"),
        feat.GetField("sink_max"),
        feat.GetField("sink_sum"),
        feat.GetField("sink_mean"),
        feat.GetField("flow_max"),
    )
eds = None
print(f"Loaded stats for {len(stats)} regions", file=sys.stderr)

# --- Open the region_id raster ---
rds = gdal.Open(region_path)
if not rds:
    print("ERROR: Could not open region_id raster", file=sys.stderr)
    sys.exit(1)
rband = rds.GetRasterBand(1)
gt = rds.GetGeoTransform()
xsize = rds.RasterXSize
ysize = rds.RasterYSize
nodata = rband.GetNoDataValue()

r_srs = osr.SpatialReference(wkt=rds.GetProjection())
r_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

# Preload the full band into memory when it is small enough (fastest path);
# otherwise fall back to per-point windowed reads.
arr = None
if xsize * ysize * 4 <= 2_000_000_000:  # ~2 GB cap for an Int32 band
    try:
        arr = rband.ReadAsArray()
        print(f"Preloaded region_id raster ({xsize} x {ysize})", file=sys.stderr)
    except Exception as e:
        print(f"Could not preload raster ({e}); using windowed reads", file=sys.stderr)
        arr = None

# --- Open NSI points read-write ---
ds = ogr.Open(gpkg_path, 1)
if not ds:
    print("ERROR: Could not open NSI GeoPackage", file=sys.stderr)
    sys.exit(1)
layer = ds.GetLayer(0)

l_srs = layer.GetSpatialRef()
if l_srs:
    l_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

ct = None
if l_srs and not l_srs.IsSame(r_srs):
    ct = osr.CoordinateTransformation(l_srs, r_srs)

count = 0
matched = 0
layer.StartTransaction()
for feat in layer:
    count += 1
    geom = feat.GetGeometryRef()
    if geom is None:
        continue

    x = geom.GetX()
    y = geom.GetY()

    # Reproject point to the raster CRS if needed
    if ct:
        try:
            x, y, _ = ct.TransformPoint(x, y, 0)
        except Exception as e:
            print(f"Transformation error: {e}", file=sys.stderr)
            continue

    px = int((x - gt[0]) / gt[1])
    py = int((y - gt[3]) / gt[5])

    if not (0 <= px < xsize and 0 <= py < ysize):
        continue

    if arr is not None:
        rid = arr[py, px]
    else:
        rid = rband.ReadAsArray(px, py, 1, 1)[0, 0]

    if nodata is not None and rid == nodata:
        continue

    rid = int(rid)
    if rid == 0:
        continue

    feat.SetField("region_id", rid)

    s = stats.get(rid)
    if s is not None:
        sc, smax, ssum, smean, fmax = s
        if sc is not None:
            feat.SetField("sink_count", int(sc))
        if smax is not None:
            feat.SetField("sink_max", float(smax))
        if ssum is not None:
            feat.SetField("sink_sum", float(ssum))
        if smean is not None:
            feat.SetField("sink_mean", float(smean))
        if fmax is not None:
            feat.SetField("flow_max", int(fmax))
        matched += 1

    layer.SetFeature(feat)

    if count % 5000 == 0:
        print(f"Processed {count} points...", file=sys.stderr)

layer.CommitTransaction()
print(f"Matched {matched}/{count} NSI points to sink regions", file=sys.stderr)

ds = None
rds = None
sys.exit(0)
PYTHON_EOF

    if ! python3 "$python_script" "$region_raster" "$sink_enriched" "$nsi_final" 2>&1; then
        warn "Raster sampling / stats join failed"
        rm -f "$python_script"
        return 1
    fi
    rm -f "$python_script"

    local feat_count
    feat_count=$(count_features "$nsi_final")
    log "Final enriched NSI structures: $feat_count features"
    return 0
}

#####################################################################
# Step 4: Join Building Class Information to NSI Structures
#####################################################################

join_building_class_to_nsi() {
    local city=$1
    local city_work_dir="${WORK_DIR}/${city}"
    local output_dir="${city_work_dir}/outputs"

    log "=== Step 4: Joining building class information to NSI ==="

    local nsi_enriched="${output_dir}/nsi_structures_enriched.gpkg"
    local nsi_final="${output_dir}/nsi_structures_enriched_with_class.gpkg"

    # Check inputs
    check_file_exists "$nsi_enriched" "Enriched NSI structures"

    # Find the building class CSV (should be in the same directory as this script)
    local script_dir
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    local building_class_csv="${script_dir}/nsi-building-class.csv"

    if [[ ! -f "$building_class_csv" ]]; then
        log "[WARNING] Building class CSV not found at $building_class_csv, skipping this step"
        return 0
    fi

    if [[ -f "$nsi_final" ]]; then
        log "Building class already joined (skipping)"
        return 0
    fi

    log "Joining building class data from CSV..."

    # Create temporary directory and GeoPackage with CSV as a table
    local temp_dir
    temp_dir=$(mktemp -d)
    local temp_gpkg="${temp_dir}/bldg_class_temp.gpkg"

    # Copy NSI layer to temporary GeoPackage with known layer name
    if ! ogr2ogr -f GPKG "$temp_gpkg" "$nsi_enriched" -nln nsi_temp 2>&1; then
        log "[WARNING] Failed to copy NSI layer to temp GeoPackage"
        rm -rf "$temp_dir"
        return 0
    fi

    # Import CSV as a table - preserve original column names
    log "Importing building class CSV..."
    ogr2ogr -f GPKG -append "$temp_gpkg" "$building_class_csv" \
        -nln bldg_class 2>/dev/null || {
        log "[WARNING] Failed to import building class CSV"
        rm -rf "$temp_dir"
        return 0
    }

    # Add diagnostics
    log "Diagnostic: Building class join setup"
    log "  NSI occtype sample values:"
    sqlite3 "$temp_gpkg" "SELECT DISTINCT occtype FROM nsi_temp LIMIT 5;" 2>/dev/null | sed 's/^/    /'
    log "  Building class nsi_occtype values:"
    sqlite3 "$temp_gpkg" "SELECT DISTINCT nsi_occtype FROM bldg_class LIMIT 5;" 2>/dev/null | sed 's/^/    /'
    log "  Building class table schema:"
    sqlite3 "$temp_gpkg" "PRAGMA table_info(bldg_class);" 2>/dev/null | sed 's/^/    /'

    # Join the tables using SQL
    # Cast columns to proper types since CSV imports everything as TEXT
    log "Performing building class join..."
    if ! ogr2ogr -f GPKG "$nsi_final" "$temp_gpkg" \
        -sql "SELECT
                n.*,
                bc.occ_desc AS building_class_desc,
                bc.risk_category
            FROM nsi_temp n
            LEFT JOIN bldg_class bc ON LOWER(n.occtype) = LOWER(bc.nsi_occtype)" \
        -nln nsi_enriched_with_class 2>/dev/null; then
        log "[WARNING] Failed to join building class information, continuing without it"
        log "Debugging: attempting simpler test query..."
        sqlite3 "$temp_gpkg" "SELECT COUNT(*) FROM nsi_temp;" 2>/dev/null | sed 's/^/  NSI records: /'
        sqlite3 "$temp_gpkg" "SELECT COUNT(*) FROM bldg_class;" 2>/dev/null | sed 's/^/  Building class records: /'
        rm -rf "$temp_dir"
        return 0
    fi

    rm -rf "$temp_dir"

    local feat_count
    feat_count=$(count_features "$nsi_final")
    log "NSI structures with building class info: $feat_count features"
    return 0
}

#####################################################################
# Main Processing Function
#####################################################################

enrich_city() {
    local city=$1
    local city_work_dir="${WORK_DIR}/${city}"
    local output_dir="${city_work_dir}/outputs"

    log "Enriching NSI structures for city: $city"
    log "Working directory: $city_work_dir"

    # Create output directory
    mkdir -p "$output_dir"

    # Run enrichment steps
    if ! extract_dem_elevations "$city"; then
        log "[ERROR] Failed to extract DEM elevations for $city"
        return 1
    fi

    if ! join_statistics_to_sinks "$city"; then
        log "[ERROR] Failed to join statistics to sinks for $city"
        return 1
    fi

    if ! sample_sink_stats_to_nsi "$city"; then
        log "[ERROR] Failed to sample sink stats to NSI for $city"
        return 1
    fi

    if ! join_building_class_to_nsi "$city"; then
        log "[ERROR] Failed to join building class information for $city"
        return 1
    fi

    log "=== Enrichment complete for $city ==="
    log "Final output files:"
    log "  - nsi_structures_elev.gpkg (NSI with DEM elevations)"
    log "  - gdal-sinks-poly_filtered_enriched.gpkg (Sinks with statistics)"
    log "  - gdal-sinks-region_id.tif (region_id raster)"
    log "  - nsi_structures_enriched.gpkg (NSI with elevations + sink attributes)"
    log "  - nsi_structures_enriched_with_class.gpkg (Final output with building class)"

    # Upload results to S3 (including the final version with building class)
    log "=== Uploading enriched NSI data to S3 ==="
    sync_to_s3 "$output_dir" "enriched_nsi/${city}"
    log "Upload complete for $city"

    # Generate presigned download links
    generate_presigned_links "$city" "$output_dir"

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

Enrich NSI building points with DEM elevations, sink/flow statistics, and building class information.

This script performs four post-processing steps:
1. Extract DEM elevations (original and filled) at NSI locations
2. Join sink and flow statistics to sink polygons
3. Rasterize region_id, sample it at NSI points, and join stats by region_id
4. Join building class descriptions and risk categories from nsi-building-class.csv

All required data is automatically downloaded from S3:
- Original DEM from s3://<bucket>/dems/<city>.tif
- Filled DEM from s3://<bucket>/sinks/<city>/dem_filled.tif
- NSI structures from s3://<bucket>/nsi/<city>/nsi_structures.gpkg
- Sink polygons and statistics from s3://<bucket>/sinks/<city>/:
  * gdal-sinks-poly_filtered.gpkg
  * gdal-sinks-stats.csv
  * gdal-flowacc-stats.csv

Building class data comes from nsi-building-class.csv (in script directory).

Enriched outputs are uploaded to s3://<bucket>/enriched_nsi/<city>/

Examples:
  $0 fargo
  $0 fargo omaha minneapolis
  $0 all                      # Process all cities in order

Environment variables:
  WORK_DIR     Working directory (default: current directory)
  VERBOSE      Enable verbose output (0=off, 1=on; default: 0)
  RASTER_RES   region_id rasterization resolution in meters (default: 4)

EOF
        exit 1
    fi

    check_prerequisites

    log "Starting NSI Enrichment"

    local failed_cities=()

    # Handle "all" special case
    if [[ "$1" == "all" ]]; then
        log "Processing all cities in preferred order..."
        for city in "${CITIES_ORDERED[@]}"; do
            if ! enrich_city "$city"; then
                log "[WARNING] Failed to process $city, continuing to next city..."
                failed_cities+=("$city")
            fi
        done
    else
        # Process specified cities
        for city in "$@"; do
            if ! enrich_city "$city"; then
                log "[WARNING] Failed to process $city, continuing to next city..."
                failed_cities+=("$city")
            fi
        done
    fi

    log "NSI Enrichment complete"

    # Print presigned links summary before exiting
    print_presigned_summary

    if [[ ${#failed_cities[@]} -gt 0 ]]; then
        log "[SUMMARY] The following cities failed to process:"
        for city in "${failed_cities[@]}"; do
            log "  - $city"
        done
        log "You can retry failed cities with: $0 ${failed_cities[*]}"
        exit 1
    else
        log "All cities enriched successfully"
        exit 0
    fi
}

main "$@"
