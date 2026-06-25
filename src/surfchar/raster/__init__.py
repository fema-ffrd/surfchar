"""Raster functions for the surfchar package."""

import numpy as np
from osgeo import gdal
from osgeo_utils import gdal_calc
import pandas as pd
from scipy import ndimage


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
