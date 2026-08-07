"""surfchar.sinks: Functions for filtering and analyzing sinks."""

from surfchar.options import SurfcharOptions

from osgeo import gdal
import pandas as pd

import os
import sys

from surfchar.utils import (
    run_cmd,
    remove_output,
    gdal_calc_creation_options,
    gdal_creation_option_args,
    get_cell_area,
    raster_grid_args,
    get_layer_feature_count,
    find_column,
)


def filter_sinks_df(sinks_df: pd.DataFrame, options: SurfcharOptions) -> pd.DataFrame:
    """
    Filter sinks based on the given filter parameters.

    Parameters
    ----------
    sinks_df : pd.DataFrame
        DataFrame containing sink information.
    options : SurfcharOptions
        Options containing filter parameters.

    Returns
    -------
    pd.DataFrame
        Filtered DataFrame containing sinks that meet the criteria.

    """
    return sinks_df.loc[
        (sinks_df["area"] > options.filter_min_area)
        & (sinks_df["avg_depth"] > options.filter_min_avg_depth)
        & (
            sinks_df["vol"] > options.filter_min_vol * 43560
        )  # convert acre-ft to cubic feet
        & (
            sinks_df["cda"] > options.filter_min_cda * 5280**2
        )  # convert square miles to square feet
        & (sinks_df["vol_cda_ratio"] > options.filter_min_vol_cda_ratio)
    ]
    
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




