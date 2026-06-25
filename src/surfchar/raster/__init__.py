"""Raster functions for the surfchar package."""

import numpy as np
from osgeo import gdal
from osgeo_utils import gdal_calc


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
        calc="A-B",
        A=filled_path,
        B=hydro_dem_path,
        outfile=output_path,
        type="Float32"
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
