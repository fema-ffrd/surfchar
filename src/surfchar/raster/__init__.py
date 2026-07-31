"""Raster functions for the surfchar package."""

# import numpy as np
# from scipy import ndimage
# from scipy.optimize import minimize_scalar

from osgeo import gdal, ogr, osr
# from osgeo_utils import gdal_calc
import pandas as pd

import os
# import xarray as xr
# from typing import Tuple

import geopandas as gpd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import unary_union, linemerge

import sys
import shutil
import subprocess
from pathlib import Path


GTIFF_CREATION_OPTIONS = [
    "TILED=YES",
    "BIGTIFF=YES",
    "COMPRESS=LZW",
]


def run_cmd(cmd: list[str]) -> None:
    """
    Run a GDAL/OGR command using list arguments.

    This is safer for Windows paths than shell=True.
    """
    print("Running:", " ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def remove_output(path: str) -> None:
    """
    Remove existing output before rewriting it.
    Handles GeoTIFF, GeoPackage, folders, and shapefile sidecars.
    """
    p = Path(path)

    if p.suffix.lower() == ".shp":
        for ext in [".shp", ".shx", ".dbf", ".prj", ".cpg", ".qix"]:
            sidecar = p.with_suffix(ext)
            if sidecar.exists():
                sidecar.unlink()
        return

    if p.exists():
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


def gdal_calc_creation_options() -> list[str]:
    return [f"--co={opt}" for opt in GTIFF_CREATION_OPTIONS]


def gdal_creation_option_args() -> list[str]:
    args = []
    for opt in GTIFF_CREATION_OPTIONS:
        args.extend(["-co", opt])
    return args


def get_cell_area(raster_path: str) -> float:
    ds = gdal.Open(raster_path)
    if ds is None:
        raise ValueError(f"Could not open raster: {raster_path}")

    gt = ds.GetGeoTransform()
    cell_area = abs(gt[1] * gt[5])

    ds = None
    return float(cell_area)


def raster_grid_args(template_raster_path: str) -> list[str]:
    """
    Return -te and -tr args matching a template raster.

    This avoids hardcoding -tr 4 4 and prevents subtle grid shifts.
    """
    ds = gdal.Open(template_raster_path)
    if ds is None:
        raise ValueError(f"Could not open template raster: {template_raster_path}")

    gt = ds.GetGeoTransform()
    width = ds.RasterXSize
    height = ds.RasterYSize

    xmin = gt[0]
    ymax = gt[3]
    xmax = xmin + width * gt[1]
    ymin = ymax + height * gt[5]

    xres = abs(gt[1])
    yres = abs(gt[5])

    ds = None

    return [
        "-te",
        str(min(xmin, xmax)),
        str(min(ymin, ymax)),
        str(max(xmin, xmax)),
        str(max(ymin, ymax)),
        "-tr",
        str(xres),
        str(yres),
    ]


def get_first_layer_name(vector_path: str) -> str:
    ds = gdal.OpenEx(vector_path)
    if ds is None:
        raise ValueError(f"Could not open vector file: {vector_path}")

    layer = ds.GetLayer(0)
    name = layer.GetName()

    ds = None
    return name


def get_layer_feature_count(vector_path: str, layer_name: str | None = None) -> int:
    ds = gdal.OpenEx(vector_path)
    if ds is None:
        raise ValueError(f"Could not open vector file: {vector_path}")

    if layer_name is None:
        layer = ds.GetLayer(0)
    else:
        layer = ds.GetLayerByName(layer_name)

    if layer is None:
        ds = None
        raise ValueError(f"Could not find layer in {vector_path}")

    count = int(layer.GetFeatureCount())
    ds = None

    return count


def find_column(df: pd.DataFrame, candidates: list[str]) -> str:
    lower_to_original = {c.lower(): c for c in df.columns}

    for candidate in candidates:
        if candidate.lower() in lower_to_original:
            return lower_to_original[candidate.lower()]

    raise KeyError(
        f"Could not find any of {candidates}. Columns found: {list(df.columns)}"
    )
    

def get_sink_depths(filled_path: str, hydro_dem_path: str, output_path: str) -> None:
    """
    Calculate sink depths by subtracting the hydro-enforced DEM from the filled DEM.

    Large-TIFF-safe:
    - no NumPy
    - no full-raster ReadAsArray
    - tiled GeoTIFF
    - BigTIFF enabled
    """
    remove_output(output_path)

    cmd = [
        sys.executable,
        "-m",
        "osgeo_utils.gdal_calc",
        "-A",
        filled_path,
        "-B",
        hydro_dem_path,
        f"--outfile={output_path}",
        "--calc=A-B",
        "--type=Float32",
        "--NoDataValue=0",
        *gdal_calc_creation_options(),
    ]

    run_cmd(cmd)
    
def check_sink_stats(raster_path: str) -> tuple[float, float]:
    """
    Check min/max using GDAL band statistics.
    Does not read the full raster into memory.
    """
    ds = gdal.Open(raster_path)
    if ds is None:
        raise ValueError(f"Could not open raster: {raster_path}")

    band = ds.GetRasterBand(1)

    stats = band.GetStatistics(False, True)
    if stats is None:
        stats = band.ComputeStatistics(False)

    min_val = float(stats[0])
    max_val = float(stats[1])

    ds = None

    return min_val, max_val

def create_sinks_mask(sink_depths_path: str, sinks_mask_path: str) -> None:
    """
    Create binary sink mask where depth > 0.
    """
    remove_output(sinks_mask_path)

    cmd = [
        sys.executable,
        "-m",
        "osgeo_utils.gdal_calc",
        "-A",
        sink_depths_path,
        f"--outfile={sinks_mask_path}",
        "--calc=A>0",
        "--type=Byte",
        "--NoDataValue=0",
        *gdal_calc_creation_options(),
    ]

    run_cmd(cmd)


def polygonize_sinks_mask(sinks_mask_path: str, sinks_poly_path: str) -> None:
    """
    Polygonize sink mask.

    -8 gives 8-connectivity, which is closest to the old scipy.ndimage.label behavior.
    """
    remove_output(sinks_poly_path)

    cmd = [
        sys.executable,
        "-m",
        "osgeo_utils.gdal_polygonize",
        "-8",
        sinks_mask_path,
        "-mask",
        sinks_mask_path,
        "-f",
        "GPKG",
        sinks_poly_path,
        "sinks",
        "DN",
    ]

    run_cmd(cmd)


def add_region_id_and_filter_sinks(
    sinks_poly_path: str,
    sinks_filtered_path: str,
    min_area: float = 2500.0,
) -> None:
    """
    Add region_id and filter out small sink polygons in vector space.
    """
    remove_output(sinks_filtered_path)

    sql = (
        "SELECT "
        "fid AS fid, "
        "fid AS region_id, "
        "DN, "
        "geom "
        "FROM sinks "
        f"WHERE DN = 1 AND ST_Area(geom) > {float(min_area)}"
    )

    cmd = [
        "ogr2ogr",
        "-overwrite",
        "-f",
        "GPKG",
        "-nln",
        "sinks_filtered",
        sinks_filtered_path,
        sinks_poly_path,
        "-dialect",
        "SQLite",
        "-sql",
        sql,
    ]

    run_cmd(cmd)


def rasterize_sink_regions(
    sinks_filtered_path: str,
    labeled_sinks_path: str,
    template_raster_path: str,
) -> None:
    """
    Rasterize filtered sink polygons to region_id raster.

    This replaces the old numpy/scipy labeled raster.
    """
    remove_output(labeled_sinks_path)

    cmd = [
        "gdal_rasterize",
        "-a",
        "region_id",
        "-where",
        "DN=1",
        "-ot",
        "Int32",
        "-a_nodata",
        "0",
        "-init",
        "0",
        *raster_grid_args(template_raster_path),
        *gdal_creation_option_args(),
        sinks_filtered_path,
        labeled_sinks_path,
    ]

    run_cmd(cmd)


def label_sinks(
    sink_depths_path: str,
    labeled_sinks_path: str,
    min_area: float = 2500.0,
) -> int:
    """
    Large-TIFF-safe replacement for scipy.ndimage.label.

    Creates:
        *_mask.tif
        *_poly.gpkg
        *_filtered.gpkg
        labeled_sinks_path

    Returns:
        number of filtered sink regions
    """
    base = os.path.splitext(labeled_sinks_path)[0]

    sinks_mask_path = f"{base}_mask.tif"
    sinks_poly_path = f"{base}_poly.gpkg"
    sinks_filtered_path = f"{base}_filtered.gpkg"

    create_sinks_mask(sink_depths_path, sinks_mask_path)
    polygonize_sinks_mask(sinks_mask_path, sinks_poly_path)

    add_region_id_and_filter_sinks(
        sinks_poly_path=sinks_poly_path,
        sinks_filtered_path=sinks_filtered_path,
        min_area=min_area,
    )

    rasterize_sink_regions(
        sinks_filtered_path=sinks_filtered_path,
        labeled_sinks_path=labeled_sinks_path,
        template_raster_path=sink_depths_path,
    )

    return get_layer_feature_count(sinks_filtered_path, "sinks_filtered")


def get_sink_stats(labeled_sinks_path: str, sink_depths_path: str) -> pd.DataFrame:
    """
    Calculate zonal statistics on sink depths using GDAL.

    Returns:
        VALUE, COUNT, AREA, MAX, MEAN, SUM
    """
    csv_path = os.path.splitext(sink_depths_path)[0] + "_sink_stats.csv"
    remove_output(csv_path)

    cmd = [
        "gdal",
        "raster",
        "zonal-stats",
        "--input",
        sink_depths_path,
        "--zones",
        labeled_sinks_path,
        "--output-format",
        "csv",
        "--output",
        csv_path,
        "--stat",
        "count",
        "--stat",
        "max",
        "--stat",
        "mean",
        "--stat",
        "sum",
        "--strategy",
        "raster",
        "--chunk-size",
        "5%",
    ]

    run_cmd(cmd)

    df = pd.read_csv(csv_path)

    zone_col = find_column(df, ["zone", "value", "region_id", "DN"])
    count_col = find_column(df, ["count"])
    max_col = find_column(df, ["max", "maximum"])
    mean_col = find_column(df, ["mean"])
    sum_col = find_column(df, ["sum"])

    cell_area = get_cell_area(labeled_sinks_path)

    sink_stats_df = pd.DataFrame(
        {
            "VALUE": df[zone_col].astype("int32"),
            "COUNT": df[count_col].astype("int64"),
            "AREA": df[count_col].astype("float64") * cell_area,
            "MAX": df[max_col].astype("float64"),
            "MEAN": df[mean_col].astype("float64"),
            "SUM": df[sum_col].astype("float64"),
        }
    )

    return sink_stats_df


def get_flowacc_stats(
    labeled_sinks_path: str,
    flowacc_path: str,
) -> pd.DataFrame:
    """
    Calculate max flow accumulation per sink using GDAL zonal stats.

    Returns:
        VALUE, max_facc
    """
    csv_path = os.path.splitext(flowacc_path)[0] + "_flowacc_stats.csv"
    remove_output(csv_path)

    cmd = [
        "gdal",
        "raster",
        "zonal-stats",
        "--input",
        flowacc_path,
        "--zones",
        labeled_sinks_path,
        "--output-format",
        "csv",
        "--output",
        csv_path,
        "--stat",
        "max",
        "--strategy",
        "raster",
        "--chunk-size",
        "5%",
    ]

    run_cmd(cmd)

    df = pd.read_csv(csv_path)

    zone_col = find_column(df, ["zone", "value", "region_id", "DN"])
    max_col = find_column(df, ["max", "maximum"])

    facc_stats_df = pd.DataFrame(
        {
            "VALUE": df[zone_col].astype("int32"),
            "max_facc": df[max_col].astype("int64"),
        }
    )

    return facc_stats_df


def build_sinks_df(
    sink_stats_df: pd.DataFrame,
    flowacc_stats_df: pd.DataFrame,
    cell_area: float,
    rainfall_inches: float,
) -> pd.DataFrame:
    """
    Build the sinks DataFrame.

    Combinine sink depth statistics, flow accumulation statistics,
    cell area, and rainfall amount.

    Parameters
    ----------
    sink_stats_df : pd.DataFrame
        DataFrame containing sink depth statistics.
    flowacc_stats_df : pd.DataFrame
        DataFrame containing flow accumulation statistics.
    cell_area : float
        Area of a single cell in the raster.
    rainfall_inches : float
        Rainfall amount in inches.

    Returns
    -------
    pd.DataFrame
        Combined DataFrame with additional calculated columns.
    """
    sinks_df = pd.DataFrame(
        {
            "id": sink_stats_df["VALUE"],
            "count": sink_stats_df["COUNT"],
            "area": sink_stats_df["AREA"],
            "max_depth": sink_stats_df["MAX"],
            "avg_depth": sink_stats_df["MEAN"],
            "vol": sink_stats_df["SUM"] * cell_area,
        }
    )
    sinks_df = sinks_df.join(flowacc_stats_df.set_index("VALUE"), on="id")
    sinks_df["cda"] = sinks_df["max_facc"] * cell_area
    sinks_df["vol_cda_ratio"] = (
        sinks_df["vol"] /
        sinks_df["cda"].replace(0, pd.NA)
    )
    sinks_df["filled_vol"] = (
        sinks_df["cda"] * rainfall_inches / 12.0
    )
    sinks_df["overflows"] = (
        sinks_df["filled_vol"] > sinks_df["vol"]
    )

    return sinks_df

def filter_sinks_vector_and_raster(
    sinks_filtered_path: str,
    selected_sinks_path: str,
    filtered_sinks_path: str,
    template_raster_path: str,
    sink_ids: pd.Series,
) -> None:
    """
    Filter selected sink IDs in vector space, then rasterize.

    This replaces filter_sinks_raster().
    """
    remove_output(selected_sinks_path)
    remove_output(filtered_sinks_path)

    ids = [int(x) for x in sink_ids.dropna().astype(int).tolist()]

    if not ids:
        raise ValueError("No sink IDs provided for filtering.")

    ids_sql = ",".join(str(x) for x in ids)

    sql = (
        "SELECT fid, region_id, DN, geom "
        "FROM sinks_filtered "
        f"WHERE region_id IN ({ids_sql})"
    )

    cmd_filter = [
        "ogr2ogr",
        "-overwrite",
        "-f",
        "GPKG",
        "-nln",
        "sinks_selected",
        selected_sinks_path,
        sinks_filtered_path,
        "-dialect",
        "SQLite",
        "-sql",
        sql,
    ]

    run_cmd(cmd_filter)

    cmd_rasterize = [
        "gdal_rasterize",
        "-a",
        "region_id",
        "-ot",
        "Int32",
        "-a_nodata",
        "0",
        "-init",
        "0",
        *raster_grid_args(template_raster_path),
        *gdal_creation_option_args(),
        selected_sinks_path,
        filtered_sinks_path,
    ]

    run_cmd(cmd_rasterize)
    
    
def get_outlet_coords(
    labeled_sinks_path: str,
    flowacc_path: str,
    sinks_df: pd.DataFrame,
):
    """
    Get outlet coordinates using GDAL zonal stats.

    Returns map coordinates directly as (x, y), not row/col.
    """
    csv_path = os.path.splitext(flowacc_path)[0] + "_flowacc_outlets.csv"
    remove_output(csv_path)

    cmd = [
        "gdal",
        "raster",
        "zonal-stats",
        "--input",
        flowacc_path,
        "--zones",
        labeled_sinks_path,
        "--output-format",
        "csv",
        "--output",
        csv_path,
        "--stat",
        "max",
        "--stat",
        "max_center_x",
        "--stat",
        "max_center_y",
        "--strategy",
        "raster",
        "--chunk-size",
        "5%",
    ]

    run_cmd(cmd)

    df = pd.read_csv(csv_path)

    zone_col = find_column(df, ["zone", "value", "region_id", "DN"])
    x_col = find_column(df, ["max_center_x"])
    y_col = find_column(df, ["max_center_y"])

    outlet_lookup = {}

    for _, row in df.iterrows():
        sink_id = int(row[zone_col])

        if pd.notna(row[x_col]) and pd.notna(row[y_col]):
            outlet_lookup[sink_id] = (float(row[x_col]), float(row[y_col]))

    outlet_coords = []

    for sink in sinks_df.itertuples():
        outlet_coords.append(outlet_lookup.get(int(sink.id)))

    return outlet_coords


def write_outlets_gpkg(
    sinks_df: pd.DataFrame,
    gpkg_path: str,
    template_raster_path: str,
    sink_id_field: str = "sink_id",
) -> None:
    """
    Write outlet points using OGR.
    """
    remove_output(gpkg_path)

    template_ds = gdal.Open(template_raster_path)
    if template_ds is None:
        raise ValueError(f"Could not open template raster: {template_raster_path}")

    projection_wkt = template_ds.GetProjection()
    template_ds = None

    srs = osr.SpatialReference()
    if projection_wkt:
        srs.ImportFromWkt(projection_wkt)

    driver = ogr.GetDriverByName("GPKG")
    out_ds = driver.CreateDataSource(gpkg_path)

    if out_ds is None:
        raise ValueError(f"Could not create GeoPackage: {gpkg_path}")

    layer = out_ds.CreateLayer("outlets", srs=srs, geom_type=ogr.wkbPoint)

    id_field = ogr.FieldDefn(sink_id_field, ogr.OFTInteger)
    layer.CreateField(id_field)

    layer_defn = layer.GetLayerDefn()

    for sink in sinks_df.itertuples():
        if sink.outlet_xy is None:
            continue

        x, y = sink.outlet_xy

        point = ogr.Geometry(ogr.wkbPoint)
        point.AddPoint(float(x), float(y))

        feature = ogr.Feature(layer_defn)
        feature.SetGeometry(point)
        feature.SetField(sink_id_field, int(sink.id))

        layer.CreateFeature(feature)

        feature = None
        point = None

    out_ds = None
    
def get_watershed_min_max_gdal(
    watersheds_path: str,
    hydro_dem_path: str,
    output_dir: str,
) -> pd.DataFrame:
    """
    Get per-watershed min and max DEM elevation using GDAL zonal stats.

    Returns:
        VALUE, MIN, MAX
    """
    os.makedirs(output_dir, exist_ok=True)

    csv_path = os.path.join(output_dir, "watershed_dem_min_max.csv")
    remove_output(csv_path)

    cmd = [
        "gdal",
        "raster",
        "zonal-stats",
        "--input",
        hydro_dem_path,
        "--zones",
        watersheds_path,
        "--output-format",
        "csv",
        "--output",
        csv_path,
        "--stat",
        "min",
        "--stat",
        "max",
        "--strategy",
        "raster",
        "--chunk-size",
        "5%",
    ]

    run_cmd(cmd)

    df = pd.read_csv(csv_path)

    zone_col = find_column(df, ["zone", "value", "region_id", "DN"])
    min_col = find_column(df, ["min", "minimum"])
    max_col = find_column(df, ["max", "maximum"])

    return pd.DataFrame(
        {
            "VALUE": df[zone_col].astype("int32"),
            "MIN": df[min_col].astype("float64"),
            "MAX": df[max_col].astype("float64"),
        }
    )
    
def make_depth_at_stage_raster(
    hydro_dem_path: str,
    stage_elev: float,
    output_path: str,
) -> None:
    """
    Create depth raster for one stage elevation.

    depth = max(stage_elev - hydro_dem, 0)
    """
    remove_output(output_path)

    calc = f"where(A < {float(stage_elev)}, {float(stage_elev)} - A, 0)"

    cmd = [
        sys.executable,
        "-m",
        "osgeo_utils.gdal_calc",
        "-A",
        hydro_dem_path,
        f"--outfile={output_path}",
        f"--calc={calc}",
        "--type=Float32",
        "--NoDataValue=0",
        *gdal_calc_creation_options(),
    ]

    run_cmd(cmd)
    
    
def get_stage_zonal_sum(
    depth_at_stage_path: str,
    watersheds_path: str,
    stage_elev: float,
    output_dir: str,
) -> pd.DataFrame:
    """
    For one stage elevation, compute depth sum by watershed.

    volume = depth_sum * cell_area
    """
    safe_stage = str(round(float(stage_elev), 3)).replace(".", "p").replace("-", "m")
    csv_path = os.path.join(output_dir, f"stage_{safe_stage}_zonal_sum.csv")
    remove_output(csv_path)

    cmd = [
        "gdal",
        "raster",
        "zonal-stats",
        "--input",
        depth_at_stage_path,
        "--zones",
        watersheds_path,
        "--output-format",
        "csv",
        "--output",
        csv_path,
        "--stat",
        "sum",
        "--strategy",
        "raster",
        "--chunk-size",
        "5%",
    ]

    run_cmd(cmd)

    df = pd.read_csv(csv_path)

    zone_col = find_column(df, ["zone", "value", "region_id", "DN"])
    sum_col = find_column(df, ["sum"])

    return pd.DataFrame(
        {
            "VALUE": df[zone_col].astype("int32"),
            "stage_elev": float(stage_elev),
            "depth_sum": df[sum_col].astype("float64"),
        }
    )
    
def get_stage_storage_table_gdal(
    watersheds_path: str,
    hydro_dem_path: str,
    sink_ids: pd.Series,
    output_dir: str,
    stage_step: float = 0.25,
) -> pd.DataFrame:
    """
    Build stage-storage table for selected watersheds using GDAL.

    Returns:
        VALUE, stage_elev, depth_sum, volume
    """
    os.makedirs(output_dir, exist_ok=True)

    sink_ids_set = set(sink_ids.dropna().astype(int).tolist())
    if not sink_ids_set:
        return pd.DataFrame(columns=["VALUE", "stage_elev", "depth_sum", "volume"])

    minmax_df = get_watershed_min_max_gdal(
        watersheds_path=watersheds_path,
        hydro_dem_path=hydro_dem_path,
        output_dir=output_dir,
    )
    
    print("minmax rows:", len(minmax_df))
    print(minmax_df.head())

    print("unique watershed ids:")
    print(minmax_df["VALUE"].head())

    print("sinks:", list(sink_ids_set)[:10])
    print("watersheds:", minmax_df["VALUE"].unique()[:10])

    # minmax_df = minmax_df[minmax_df["VALUE"].isin(sink_ids_set)].copy()

    if minmax_df.empty:
        return pd.DataFrame(columns=["VALUE", "stage_elev", "depth_sum", "volume"])

    global_min = float(minmax_df["MIN"].min())
    global_max = float(minmax_df["MAX"].max())

    start_stage = stage_step * int(global_min // stage_step)
    end_stage = stage_step * int(global_max // stage_step + 1)

    stages = []
    z = start_stage
    while z <= end_stage:
        stages.append(round(z, 6))
        z += stage_step

    cell_area = get_cell_area(hydro_dem_path)

    all_rows = []
    temp_depth_path = os.path.join(output_dir, "_depth_at_stage_tmp.tif")

    for stage_elev in stages:
        make_depth_at_stage_raster(
            hydro_dem_path=hydro_dem_path,
            stage_elev=stage_elev,
            output_path=temp_depth_path,
        )

        stage_df = get_stage_zonal_sum(
            depth_at_stage_path=temp_depth_path,
            watersheds_path=watersheds_path,
            stage_elev=stage_elev,
            output_dir=output_dir,
        )

        # stage_df = stage_df[stage_df["VALUE"].isin(sink_ids_set)].copy()
        stage_df["volume"] = stage_df["depth_sum"] * cell_area

        all_rows.append(stage_df)

    remove_output(temp_depth_path)

    if not all_rows:
        return pd.DataFrame(columns=["VALUE", "stage_elev", "depth_sum", "volume"])

    stage_storage_df = pd.concat(all_rows, ignore_index=True)

    stage_storage_csv = os.path.join(output_dir, "stage_storage_table.csv")
    stage_storage_df.to_csv(stage_storage_csv, index=False)

    return stage_storage_df

def get_fill_elevs_from_stage_storage(
    stage_storage_df: pd.DataFrame,
    sinks_df: pd.DataFrame,
    rainfall_ft: float,
) -> pd.DataFrame:
    """
    Determine fill elevation from stage-storage curves.

    stage_storage_df["VALUE"] should match sinks_df["watershed_id"].
    Results are merged back to sinks_df using original sink id.
    """
    fill_rows = []

    if "watershed_id" not in sinks_df.columns:
        raise ValueError("sinks_df is missing watershed_id. Add it before stage storage.")

    valid_sinks_df = sinks_df[pd.notna(sinks_df["watershed_id"])].copy()

    target_lookup = {
        int(row.watershed_id): {
            "sink_id": int(row.id),
            "target_volume": float(row.cda) * float(rainfall_ft),
        }
        for row in valid_sinks_df.itertuples()
    }

    for watershed_id, group in stage_storage_df.groupby("VALUE"):
        watershed_id = int(watershed_id)

        if watershed_id not in target_lookup:
            continue

        sink_id = target_lookup[watershed_id]["sink_id"]
        target_volume = target_lookup[watershed_id]["target_volume"]

        g = group.sort_values("stage_elev").copy()

        if g.empty:
            fill_rows.append(
                {
                    "id": sink_id,
                    "fill_elev": pd.NA,
                    "target_volume": target_volume,
                }
            )
            continue

        if float(g["volume"].max()) < target_volume:
            fill_rows.append(
                {
                    "id": sink_id,
                    "fill_elev": pd.NA,
                    "target_volume": target_volume,
                }
            )
            continue

        above = g[g["volume"] >= target_volume].head(1)

        if above.empty:
            fill_elev = pd.NA
        else:
            idx_above = above.index[0]
            pos_above = g.index.get_loc(idx_above)

            if pos_above == 0:
                fill_elev = float(g.iloc[0]["stage_elev"])
            else:
                low = g.iloc[pos_above - 1]
                high = g.iloc[pos_above]

                z0 = float(low["stage_elev"])
                z1 = float(high["stage_elev"])
                v0 = float(low["volume"])
                v1 = float(high["volume"])

                if v1 == v0:
                    fill_elev = z1
                else:
                    frac = (target_volume - v0) / (v1 - v0)
                    fill_elev = z0 + frac * (z1 - z0)

        fill_rows.append(
            {
                "id": sink_id,
                "fill_elev": fill_elev,
                "target_volume": target_volume,
            }
        )

    fill_df = pd.DataFrame(fill_rows)

    print("fill_rows:", len(fill_rows))
    print("fill_df columns:", fill_df.columns.tolist())

    if fill_df.empty:
        sinks_df["fill_elev"] = pd.NA
        sinks_df["target_volume"] = pd.NA
        return sinks_df

    return sinks_df.merge(fill_df, on="id", how="left")

# temp helper function
def get_watershed_id_at_outlet(
        watersheds_path,
        x,
        y,
    ):
    ds = gdal.Open(watersheds_path)

    gt = ds.GetGeoTransform()
    band = ds.GetRasterBand(1)

    col = int((x - gt[0]) / gt[1])
    row = int((y - gt[3]) / gt[5])

    value = band.ReadAsArray(col, row, 1, 1)[0, 0]

    ds = None

    return int(value)


def rasterize_fill_elevation(
    watersheds_fill_elev_vector_path: str,
    fill_elev_raster_path: str,
    template_raster_path: str,
) -> None:
    """
    Rasterize fill_elev attribute to a Float32 raster.
    """
    remove_output(fill_elev_raster_path)

    cmd = [
        "gdal_rasterize",
        "-a",
        "fill_elev",
        "-ot",
        "Float32",
        "-a_nodata",
        "0",
        "-init",
        "0",
        *raster_grid_args(template_raster_path),
        *gdal_creation_option_args(),
        watersheds_fill_elev_vector_path,
        fill_elev_raster_path,
    ]

    run_cmd(cmd)
    
def map_watershed_fill_raster_gdal(
    hydro_dem_path: str,
    watersheds_id_raster_path: str,
    watersheds_fill_elev_vector_path: str,
    output_path: str,
    output_dir: str,
) -> None:
    """
    Create filled area raster without NumPy.

    Output:
        watershed ID where hydro_dem < fill_elev
        0 elsewhere
    """
    os.makedirs(output_dir, exist_ok=True)

    fill_elev_raster_path = os.path.join(output_dir, "fill_elev_raster.tif")

    rasterize_fill_elevation(
        watersheds_fill_elev_vector_path=watersheds_fill_elev_vector_path,
        fill_elev_raster_path=fill_elev_raster_path,
        template_raster_path=hydro_dem_path,
    )

    remove_output(output_path)

    calc = "where((B > 0) * (A < B) * (C > 0), C, 0)"

    cmd = [
        sys.executable,
        "-m",
        "osgeo_utils.gdal_calc",
        "-A",
        hydro_dem_path,
        "-B",
        fill_elev_raster_path,
        "-C",
        watersheds_id_raster_path,
        f"--outfile={output_path}",
        f"--calc={calc}",
        "--type=Int32",
        "--NoDataValue=0",
        *gdal_calc_creation_options(),
    ]

    run_cmd(cmd)
    
def write_fill_elev_csv(
    sinks_df: pd.DataFrame,
    csv_path: str,
    id_field: str = "watershed_id",
) -> None:
    remove_output(csv_path)

    out = sinks_df[[id_field, "fill_elev"]].copy()
    out = out[pd.notna(out["fill_elev"])].copy()
    out = out.rename(columns={id_field: "id"})

    out.to_csv(csv_path, index=False)
    
def join_fill_elev_to_watersheds(
    watersheds_vector_path: str,
    fill_elev_csv: str,
    output_vector_path: str,
    watershed_id_field: str = "gridcode",
) -> None:
    """
    Join fill_elev CSV to watershed polygons.
    """

    remove_output(output_vector_path)

    watersheds_gdf = gpd.read_file(watersheds_vector_path)

    fill_df = pd.read_csv(fill_elev_csv)

    watersheds_gdf[watershed_id_field] = (
        watersheds_gdf[watershed_id_field]
        .astype("int64")
    )

    fill_df["id"] = fill_df["id"].astype("int64")

    out_gdf = watersheds_gdf.merge(
        fill_df,
        left_on=watershed_id_field,
        right_on="id",
        how="left",
    )

    out_gdf = out_gdf[
        pd.notna(out_gdf["fill_elev"])
    ].copy()

    out_gdf.to_file(
        output_vector_path,
        driver="GPKG",
    )
    


    
def raster_to_polygons(
    raster_path: str,
    output_path: str,
) -> None:

    src_ds = gdal.Open(raster_path)
    band = src_ds.GetRasterBand(1)

    ext = os.path.splitext(output_path)[1].lower()

    if ext == ".shp":
        driver_name = "ESRI Shapefile"
    elif ext == ".gpkg":
        driver_name = "GPKG"
    else:
        raise ValueError(f"Unsupported output format: {ext}")

    driver = ogr.GetDriverByName(driver_name)

    if os.path.exists(output_path):
        driver.DeleteDataSource(output_path)

    out_ds = driver.CreateDataSource(output_path)

    srs = osr.SpatialReference()
    srs.ImportFromWkt(src_ds.GetProjection())

    layer_name = os.path.splitext(os.path.basename(output_path))[0]

    layer = out_ds.CreateLayer(
        layer_name,
        srs=srs,
        geom_type=ogr.wkbMultiPolygon,
    )

    field_defn = ogr.FieldDefn("gridcode", ogr.OFTInteger)
    layer.CreateField(field_defn)

    gdal.Polygonize(
        band,
        None,
        layer,
        0,
        [],
    )

    out_ds = None
    src_ds = None

def buffer_polygons(
    input_path: str,
    output_path: str,
    distance: float,
) -> None:

    gdf = gpd.read_file(input_path)

    gdf["geometry"] = gdf.buffer(distance)

    ext = os.path.splitext(output_path)[1].lower()

    if ext == ".shp":
        driver = "ESRI Shapefile"

        base = os.path.splitext(output_path)[0]
        for sidecar_ext in [".shp", ".shx", ".dbf", ".prj", ".cpg"]:
            sidecar = base + sidecar_ext
            if os.path.exists(sidecar):
                os.remove(sidecar)

    elif ext == ".gpkg":
        driver = "GPKG"

        if os.path.exists(output_path):
            os.remove(output_path)

    else:
        raise ValueError(f"Unsupported output format: {ext}")

    gdf.to_file(
        output_path,
        driver=driver,
    )
    
def polygons_to_lines(
    input_path: str,
    output_path: str,
) -> None:

    gdf = gpd.read_file(input_path)

    lines = gdf.boundary

    out_gdf = gpd.GeoDataFrame(
        gdf.drop(columns="geometry"),
        geometry=lines,
        crs=gdf.crs,
    )

    out_gdf.to_file(
        output_path,
        driver="ESRI Shapefile",
    )


def clip_watersheds(
    watersheds: str,
    sinks_buffer: str,
    sinks_that_overflow: pd.Series,
    watersheds_clipped: str,
    watersheds_fill_buffer: str = None,
    clip_all: bool = False,
    min_breakline_length: float = 200.0,
):

    watersheds_gdf = gpd.read_file(watersheds)
    sinks_buffer_gdf = gpd.read_file(sinks_buffer)

    # use gridcode as lookup key
    buffer_lookup = (
        sinks_buffer_gdf
        .set_index("gridcode")
        .geometry
        .to_dict()
    )

    # replace with larger fill buffer if available
    if watersheds_fill_buffer:
        fill_buffer_gdf = gpd.read_file(watersheds_fill_buffer)

        for row in fill_buffer_gdf.itertuples():
            sink_id = row.gridcode

            if sink_id not in buffer_lookup:
                continue

            if row.geometry.area > buffer_lookup[sink_id].area:
                buffer_lookup[sink_id] = row.geometry

    output_rows = []

    overflow_ids = set(sinks_that_overflow)

    for row in watersheds_gdf.itertuples():

        watershed_id = row.gridcode
        watershed_line = row.geometry

        # not clipped
        if not clip_all and watershed_id not in overflow_ids:
            output_rows.append(
                {
                    "gridcode": watershed_id,
                    "geometry": watershed_line,
                }
            )
            continue

        if watershed_id not in buffer_lookup:
            continue

        clip_poly = buffer_lookup[watershed_id]

        clipped = watershed_line.intersection(clip_poly)

        if clipped.is_empty:
            continue

        if isinstance(clipped, LineString):

            if clipped.length >= min_breakline_length:
                output_rows.append(
                    {
                        "gridcode": watershed_id,
                        "geometry": clipped,
                    }
                )

        elif isinstance(clipped, MultiLineString):

            for geom in clipped.geoms:

                if geom.length >= min_breakline_length:
                    output_rows.append(
                        {
                            "gridcode": watershed_id,
                            "geometry": geom,
                        }
                    )

    out_gdf = gpd.GeoDataFrame(
        output_rows,
        geometry="geometry",
        crs=watersheds_gdf.crs,
    )

    out_gdf.to_file(
        watersheds_clipped,
        driver="ESRI Shapefile",
    )
    

def dissolve_breaklines(
    watersheds_lines_clipped: str,
):
    gdf = gpd.read_file(watersheds_lines_clipped)

    merged = unary_union(gdf.geometry)

    merged = linemerge(merged)

    if isinstance(merged, LineString):
        return [merged]

    elif isinstance(merged, MultiLineString):
        return list(merged.geoms)

    return []

def fix_self_closing_breaklines(
    breaklines,
):
    fixed = []

    for line in breaklines:

        if not isinstance(line, LineString):
            continue

        coords = list(line.coords)

        if len(coords) < 4:
            fixed.append(line)
            continue

        # first/last point equal
        if coords[0] == coords[-1]:

            midpoint = len(coords) // 2

            first = LineString(coords[: midpoint + 1])
            second = LineString(coords[midpoint:])

            fixed.extend([first, second])

        else:
            fixed.append(line)

    return fixed

def write_breaklines_shapefile(
    breaklines,
    shapefile_path: str,
    crs = None,
):

    rows = []

    for line in breaklines:
        rows.append(
            {
                "length_ft": float(line.length),
                "geometry": line,
            }
        )

    gdf = gpd.GeoDataFrame(
        rows,
        geometry="geometry",
        crs = crs, 
    )

    gdf.to_file(
        shapefile_path,
        driver="ESRI Shapefile",
    )