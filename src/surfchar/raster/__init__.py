"""Raster functions for the surfchar package."""

from osgeo import gdal, ogr, osr
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
    find_column,
)



    
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
    Join fill_elev CSV to watershed polygons using OGR.
    """

    remove_output(output_vector_path)

    fill_df = pd.read_csv(fill_elev_csv)

    fill_lookup = dict(
        zip(
            fill_df["id"].astype(int),
            fill_df["fill_elev"].astype(float),
        )
    )

    src_ds = ogr.Open(watersheds_vector_path)
    if src_ds is None:
        raise ValueError(
            f"Could not open vector: {watersheds_vector_path}"
        )

    src_layer = src_ds.GetLayer()

    driver = ogr.GetDriverByName("GPKG")

    out_ds = driver.CreateDataSource(output_vector_path)

    out_layer = out_ds.CreateLayer(
        src_layer.GetName(),
        srs=src_layer.GetSpatialRef(),
        geom_type=ogr.wkbMultiPolygon,
    )

    # copy original fields
    src_defn = src_layer.GetLayerDefn()

    for i in range(src_defn.GetFieldCount()):
        field_defn = src_defn.GetFieldDefn(i)
        out_layer.CreateField(field_defn)

    # add fill_elev field
    out_layer.CreateField(
        ogr.FieldDefn("fill_elev", ogr.OFTReal)
    )

    out_defn = out_layer.GetLayerDefn()

    for feature in src_layer:

        watershed_id = feature.GetField(
            watershed_id_field
        )

        if watershed_id is None:
            continue

        watershed_id = int(watershed_id)

        if watershed_id not in fill_lookup:
            continue

        out_feature = ogr.Feature(out_defn)

        out_feature.SetGeometry(
            feature.GetGeometryRef().Clone()
        )

        # copy existing attributes
        for i in range(src_defn.GetFieldCount()):

            field_name = src_defn.GetFieldDefn(i).GetNameRef()

            out_feature.SetField(
                field_name,
                feature.GetField(field_name),
            )

        out_feature.SetField(
            "fill_elev",
            float(fill_lookup[watershed_id]),
        )

        out_layer.CreateFeature(out_feature)

        out_feature = None

    out_ds = None
    src_ds = None
    
    
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




    