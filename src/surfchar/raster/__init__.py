"""Raster functions for the surfchar package."""

import numpy as np
from osgeo import gdal, ogr, osr
from osgeo_utils import gdal_calc
import pandas as pd
from scipy import ndimage
import os
# import xarray as xr
from typing import Tuple
from scipy.optimize import minimize_scalar
import geopandas as gpd
from shapely.geometry import Point, LineString, MultiLineString
from shapely.ops import unary_union, linemerge


def get_sink_depths(filled_path: str, hydro_dem_path: str, output_path) -> None:
    """Calculate sink depths by subtracting the hydro-enforced DEM from the filled DEM.

    Parameters
    ----------
    filled_path : str
        Path to the filled DEM raster file.
    hydro_dem_path : str
        Path to the hydro-enforced DEM raster file.
    output_path : str
        Path to the output sink depths raster file.
    """
    gdal_calc.Calc(
        calc="A-B", A=filled_path, B=hydro_dem_path, outfile=output_path, type="Float32"
    )


def check_sink_stats(raster_path: str) -> tuple[float, float]:
    """Check the minimum and maximum values of a raster, ignoring nodata values.

    Parameters
    ----------
    raster_path : str
        Path to the raster file.

    Returns
    -------
    tuple[float, float]
        Minimum and maximum values of the raster.
    """
    ds = gdal.Open(raster_path)
    band = ds.GetRasterBand(1)

    arr = band.ReadAsArray().astype(float)

    # Handle nodata
    nodata = band.GetNoDataValue()
    if nodata is not None:
        arr[arr == nodata] = np.nan

    min_val = np.nanmin(arr)
    max_val = np.nanmax(arr)

    return min_val, max_val


def label_sinks(sink_depths_path: str, labeled_sinks_path: str) -> int:
    """
    Label connected sink regions in the sink depths raster.

    Parameters
    ----------
    sink_depths_path : str
        Path to the sink depths raster file.
    labeled_sinks_path : str
        Path to the output labeled sinks raster file.

    Returns
    -------
    int: Number of unique sink regions found.
    """
    ds = gdal.Open(sink_depths_path)
    if ds is None:
        raise ValueError(f"Could not open sink depths raster: {sink_depths_path}")

    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()

    nodata = band.GetNoDataValue()

    # Build binary mask: positive sink depths are foreground, everything else background
    mask = arr > 0
    if nodata is not None:
        mask = mask & (arr != nodata)

    # 8-connectivity: center pixel plus all 8 neighbors
    structure: np.ndarray = np.ones((3, 3), dtype=np.uint8)

    labeled, num_features = ndimage.label(mask, structure=structure)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        labeled_sinks_path, ds.RasterXSize, ds.RasterYSize, 1, gdal.GDT_Int32
    )

    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())

    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(labeled.astype(np.int32))
    out_band.SetNoDataValue(0)  # background = 0
    out_band.FlushCache()

    out_ds = None
    ds = None

    return num_features


def get_sink_stats(labeled_sinks_path: str, sink_depths_path: str) -> pd.DataFrame:
    """
    Calculate zonal statistics on the sink depths raster using a labeled sink raster.

    Returns a DataFrame with ArcPy-like uppercase columns:
    VALUE, COUNT, AREA, MAX, MEAN, SUM
    """
    labels_ds = gdal.Open(labeled_sinks_path)
    depths_ds = gdal.Open(sink_depths_path)

    if labels_ds is None:
        raise ValueError(f"Could not open labeled sinks raster: {labeled_sinks_path}")
    if depths_ds is None:
        raise ValueError(f"Could not open sink depths raster: {sink_depths_path}")

    labels = labels_ds.GetRasterBand(1).ReadAsArray()
    depths = depths_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)

    labels_band = labels_ds.GetRasterBand(1)
    depths_band = depths_ds.GetRasterBand(1)

    labels_nodata = labels_band.GetNoDataValue()
    depths_nodata = depths_band.GetNoDataValue()

    # valid data mask
    valid = labels > 0
    if labels_nodata is not None:
        valid &= labels != labels_nodata
    if depths_nodata is not None:
        valid &= depths != depths_nodata

    # labels to summarize
    sink_ids = np.unique(labels[valid])
    sink_ids = sink_ids[sink_ids > 0]

    if sink_ids.size == 0:
        return pd.DataFrame(columns=["VALUE", "COUNT", "AREA", "MAX", "MEAN", "SUM"])

    # area per cell from geotransform
    gt = labels_ds.GetGeoTransform()
    cell_area = abs(gt[1] * gt[5])

    # Use only valid pixels
    valid_labels = labels[valid]
    valid_depths = depths[valid]

    # COUNT: number of cells in each sink
    counts_all = np.bincount(valid_labels.ravel())
    counts = counts_all[sink_ids]

    # Region stats
    sums = ndimage.sum(valid_depths, labels=valid_labels, index=sink_ids)
    means = ndimage.mean(valid_depths, labels=valid_labels, index=sink_ids)
    mins = ndimage.minimum(valid_depths, labels=valid_labels, index=sink_ids)
    maxs = ndimage.maximum(valid_depths, labels=valid_labels, index=sink_ids)

    sink_stats_df = pd.DataFrame(
        {
            "VALUE": sink_ids.astype(np.int32),
            "COUNT": counts.astype(np.int32),
            "AREA": counts.astype(np.float64) * cell_area,
            "MIN": np.asarray(mins, dtype=np.float64),
            "MAX": np.asarray(maxs, dtype=np.float64),
            "MEAN": np.asarray(means, dtype=np.float64),
            "SUM": np.asarray(sums, dtype=np.float64),
        }
    )

    labels_ds = None
    depths_ds = None

    return sink_stats_df


def get_flowacc_stats(labeled_sinks_path: str, flowacc_path: str) -> pd.DataFrame:
    """
    Calculate per-sink maximum flow accumulation using a labeled sink raster.

    Returns columns:
        VALUE, max_facc
    """
    labels_ds = gdal.Open(labeled_sinks_path)
    flowacc_ds = gdal.Open(flowacc_path)

    if labels_ds is None:
        raise ValueError(f"Could not open labeled sinks raster: {labeled_sinks_path}")
    if flowacc_ds is None:
        raise ValueError(f"Could not open flow accumulation raster: {flowacc_path}")

    labels = labels_ds.GetRasterBand(1).ReadAsArray()
    flowacc = flowacc_ds.GetRasterBand(1).ReadAsArray()

    labels_band = labels_ds.GetRasterBand(1)
    flowacc_band = flowacc_ds.GetRasterBand(1)

    labels_nodata = labels_band.GetNoDataValue()
    flowacc_nodata = flowacc_band.GetNoDataValue()

    valid = labels > 0
    if labels_nodata is not None:
        valid &= labels != labels_nodata
    if flowacc_nodata is not None:
        valid &= flowacc != flowacc_nodata

    sink_ids = np.unique(labels[valid])
    sink_ids = sink_ids[sink_ids > 0]

    if sink_ids.size == 0:
        return pd.DataFrame(columns=["VALUE", "max_facc"])

    valid_labels = labels[valid]
    valid_flowacc = flowacc[valid]

    max_facc = ndimage.maximum(valid_flowacc, labels=valid_labels, index=sink_ids)

    facc_stats_df = pd.DataFrame(
        {
            "VALUE": sink_ids.astype(np.int32),
            "max_facc": np.asarray(max_facc).astype(np.int32),
        }
    )

    labels_ds = None
    flowacc_ds = None
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
    sinks_df["vol_cda_ratio"] = sinks_df["vol"] / sinks_df["cda"]
    sinks_df["filled_vol"] = sinks_df["cda"] * rainfall_inches / 12.0
    sinks_df["overflows"] = [
        True if sink.filled_vol > sink.vol else False for sink in sinks_df.itertuples()
    ]
    return sinks_df

def filter_sinks_raster(
    labeled_sinks_path: str,
    filtered_sinks_path: str,
    sink_ids: pd.Series,
) -> None:
    """
    Keep only selected sink IDs in the labeled sinks raster.

    Input labeled sink raster:
        0 = background
        1, 2, 3, ... = sink IDs

    Output raster:
        selected sink IDs are preserved
        everything else becomes 0
    """
    ds = gdal.Open(labeled_sinks_path, gdal.GA_ReadOnly)
    if ds is None:
        raise ValueError(f"Could not open labeled sinks raster: {labeled_sinks_path}")

    band = ds.GetRasterBand(1)
    labeled = band.ReadAsArray()

    sink_ids_set = set(sink_ids.astype(int).tolist())

    filtered = np.where(
        np.isin(labeled, list(sink_ids_set)),
        labeled,
        0,
    ).astype(np.int32)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        filtered_sinks_path,
        ds.RasterXSize,
        ds.RasterYSize,
        1,
        gdal.GDT_Int32,
        options=["COMPRESS=LZW"],
    )

    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())

    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(filtered)
    out_band.SetNoDataValue(0)
    out_band.FlushCache()

    out_ds = None
    ds = None
    
def get_outlet_coords(
    labeled_sinks_path: str,
    flowacc_path: str,
    sinks_df: pd.DataFrame,
):
    labels_ds = gdal.Open(labeled_sinks_path)
    flowacc_ds = gdal.Open(flowacc_path)

    labels = labels_ds.GetRasterBand(1).ReadAsArray()
    flowacc = flowacc_ds.GetRasterBand(1).ReadAsArray()

    outlet_coords = []

    for sink in sinks_df.itertuples():
        matches = np.argwhere(
            (labels == sink.id) &
            (flowacc == sink.max_facc)
        )

        if len(matches) == 0:
            outlet_coords.append(None)
            continue

        row, col = matches[0]
        outlet_coords.append((int(row), int(col)))

    return outlet_coords

def row_col_to_x_y(row_col: Tuple[int, int], x_min: float, y_max: float, cell_width: float, cell_height: float)\
        -> Tuple[float, float]:
    """
    Convert a (row, column) coordinate pair to an (x, y) pair.
    """
    row, col = row_col
    dx = col * cell_width + cell_width / 2
    dy = row * cell_height + cell_height / 2
    x = x_min + dx
    y = y_max - dy
    return x, y

def write_outlets_gpkg(
    sinks_df: pd.DataFrame,
    gpkg_path: str,
    template_raster_path: str,
    sink_id_field: str = "sink_id",
) -> None:

    rows = []

    for sink in sinks_df.itertuples():
        if sink.outlet_xy is None:
            continue

        x, y = sink.outlet_xy

        rows.append(
            {
                sink_id_field: int(sink.id),
                "geometry": Point(x, y),
            }
        )

    template_ds = gdal.Open(template_raster_path)
    wkt = template_ds.GetProjection()
    template_ds = None

    gdf = gpd.GeoDataFrame(
        rows,
        geometry="geometry",
    )

    gdf.set_crs(wkt, inplace=True)

    gdf.to_file(
        gpkg_path,
        driver="GPKG",
        layer="outlets",
    )
    
# In case someday we need shapefile
# def write_outlets_shapefile(
#     sinks_df: pd.DataFrame,
#     shapefile_path: str,
#     template_raster_path: str,
#     sink_id_field: str = "sink_id",
# ) -> None:
#     """
#     Write sink outlet points to a shapefile using GDAL/OGR.

#     Parameters
#     ----------
#     sinks_df : pd.DataFrame
#         DataFrame containing outlet_xy and id columns.
#     shapefile_path : str
#         Output shapefile path.
#     template_raster_path : str
#         Raster used to copy projection.
#     sink_id_field : str
#         Field name for sink ID.
#     """

#     template_ds = gdal.Open(template_raster_path)
#     if template_ds is None:
#         raise ValueError(f"Could not open template raster: {template_raster_path}")

#     projection_wkt = template_ds.GetProjection()
#     template_ds = None

#     spatial_ref = osr.SpatialReference()
#     if projection_wkt:
#         spatial_ref.ImportFromWkt(projection_wkt)

#     driver = ogr.GetDriverByName("ESRI Shapefile")

#     if os.path.exists(shapefile_path):
#         driver.DeleteDataSource(shapefile_path)

#     out_ds = driver.CreateDataSource(shapefile_path)
#     if out_ds is None:
#         raise ValueError(f"Could not create shapefile: {shapefile_path}")

#     layer = out_ds.CreateLayer(
#         os.path.splitext(os.path.basename(shapefile_path))[0],
#         spatial_ref,
#         ogr.wkbPoint,
#     )

#     id_field = ogr.FieldDefn(sink_id_field, ogr.OFTInteger)
#     layer.CreateField(id_field)

#     layer_defn = layer.GetLayerDefn()

#     for sink in sinks_df.itertuples():
#         if sink.outlet_xy is None:
#             continue

#         x, y = sink.outlet_xy

#         point = ogr.Geometry(ogr.wkbPoint)
#         point.AddPoint(float(x), float(y))

#         feature = ogr.Feature(layer_defn)
#         feature.SetGeometry(point)
#         feature.SetField(sink_id_field, int(sink.id))

#         layer.CreateFeature(feature)

#         feature = None
#         point = None

#     out_ds = None
    
def get_watershed_dems(
    hydro_dem_path: str,
    watersheds_path: str,
    sink_ids: pd.Series,
) -> list[np.ndarray]:
    """
    Extract DEM values for each sink watershed.
    """

    hydro_ds = gdal.Open(hydro_dem_path)
    watersheds_ds = gdal.Open(watersheds_path)

    if hydro_ds is None:
        raise ValueError(f"Could not open hydro DEM: {hydro_dem_path}")

    if watersheds_ds is None:
        raise ValueError(f"Could not open watersheds raster: {watersheds_path}")

    hydro = hydro_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)
    watersheds = watersheds_ds.GetRasterBand(1).ReadAsArray()

    hydro_nodata = hydro_ds.GetRasterBand(1).GetNoDataValue()

    watershed_dems = []

    for sink_id in sink_ids.astype(int):
        mask = watersheds == sink_id

        dem_values = hydro[mask]

        if hydro_nodata is not None:
            dem_values = dem_values[dem_values != hydro_nodata]

        watershed_dems.append(dem_values)

    hydro_ds = None
    watersheds_ds = None

    return watershed_dems


def watershed_volume_objective_function(
    elevation: float,
    watershed_dem: np.ndarray,
    target_volume: float,
    cell_area: float,
) -> float:
    """
    Calculate difference between stored volume and target volume.
    """

    dem_diff = elevation - watershed_dem
    dem_diff_vol = dem_diff[dem_diff > 0].sum() * cell_area

    return abs(dem_diff_vol - target_volume)


def get_watershed_fill_elev(
    sink,
    watershed_dem: np.ndarray,
    rainfall_ft: float,
    cell_area: float,
) -> float:
    """
    Determine fill elevation for one sink watershed.
    """

    if watershed_dem.size == 0:
        return np.nan

    target_volume = sink.cda * rainfall_ft

    bounds = (
        float(np.nanmin(watershed_dem)),
        float(np.nanmax(watershed_dem)),
    )

    solution = minimize_scalar(
        watershed_volume_objective_function,
        args=(watershed_dem, target_volume, cell_area),
        method="bounded",
        bounds=bounds,
    )

    return float(solution.x)


def get_watershed_fill_elevs(
    watershed_dems: list[np.ndarray],
    sinks_df: pd.DataFrame,
    rainfall_ft: float,
    cell_area: float,
) -> list:
    """
    Calculate fill elevations for all sink watersheds.
    """

    fill_elevs = []

    for sink, watershed_dem in zip(sinks_df.itertuples(), watershed_dems):
        fill_elev = get_watershed_fill_elev(
            sink=sink,
            watershed_dem=watershed_dem,
            rainfall_ft=rainfall_ft,
            cell_area=cell_area,
        )

        fill_elevs.append(fill_elev)

    return fill_elevs


def map_watershed_fill_raster(
    watersheds_path: str,
    hydro_dem_path: str,
    sinks_df: pd.DataFrame,
    output_path: str,
) -> None:
    """
    Create raster showing where watershed DEM is below each sink fill elevation.

    Output raster:
        0 = background
        sink ID = filled area for that sink
    """

    watersheds_ds = gdal.Open(watersheds_path)
    hydro_ds = gdal.Open(hydro_dem_path)

    if watersheds_ds is None:
        raise ValueError(f"Could not open watersheds raster: {watersheds_path}")

    if hydro_ds is None:
        raise ValueError(f"Could not open hydro DEM: {hydro_dem_path}")

    watersheds = watersheds_ds.GetRasterBand(1).ReadAsArray()
    hydro = hydro_ds.GetRasterBand(1).ReadAsArray().astype(np.float64)

    output = np.zeros(watersheds.shape, dtype=np.int32)

    for sink in sinks_df.itertuples():
        if pd.isna(sink.fill_elev):
            continue

        mask = (
            (watersheds == sink.id)
            & (hydro < sink.fill_elev)
        )

        output[mask] = int(sink.id)

    driver = gdal.GetDriverByName("GTiff")

    out_ds = driver.Create(
        output_path,
        watersheds_ds.RasterXSize,
        watersheds_ds.RasterYSize,
        1,
        gdal.GDT_Int32,
        options=["COMPRESS=LZW"],
    )

    out_ds.SetGeoTransform(watersheds_ds.GetGeoTransform())
    out_ds.SetProjection(watersheds_ds.GetProjection())

    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(output)
    out_band.SetNoDataValue(0)
    out_band.FlushCache()

    out_ds = None
    watersheds_ds = None
    hydro_ds = None
    
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
    )

    gdf.to_file(
        shapefile_path,
        driver="ESRI Shapefile",
    )