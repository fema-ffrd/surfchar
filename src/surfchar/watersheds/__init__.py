"""Functions for creating and analyzing sink watersheds."""

from osgeo import gdal, ogr, osr
import pandas as pd
import os

from surfchar.utils import (
    run_cmd,
    remove_output,
    find_column,
)

  
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



