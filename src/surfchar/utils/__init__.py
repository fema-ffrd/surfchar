"""Utility functions for the surfchar package."""

from osgeo import gdal
from typing import List


def _compare_raster_dimensions(raster1_path: str, raster2_path: str) -> bool:
    """
    Compare the dimensions (rows and columns) of two rasters.

    Parameters
    ----------
    raster1_path : str
        Path to the first raster file.
    raster2_path : str
        Path to the second raster file.

    Returns
    -------
    bool: True if the rasters have the same dimensions, False otherwise.
    """
    # Open the first raster
    raster1 = gdal.Open(raster1_path)
    if raster1 is None:
        raise ValueError(f"Could not open raster: {raster1_path}")

    # Open the second raster
    raster2 = gdal.Open(raster2_path)
    if raster2 is None:
        raise ValueError(f"Could not open raster: {raster2_path}")

    # Get dimensions of both rasters
    cols1, rows1 = raster1.RasterXSize, raster1.RasterYSize
    cols2, rows2 = raster2.RasterXSize, raster2.RasterYSize

    # Close the rasters
    raster1 = None
    raster2 = None

    # Compare dimensions
    return (cols1 == cols2) and (rows1 == rows2)


def raster_sizes_match(raster_paths: List[str]) -> bool:
    """
    Check if two rasters have the same dimensions (rows and columns).

    Parameters
    ----------
    raster_paths : List[str]
        List of paths to the raster files.

    Returns
    -------
    bool: True if the rasters have the same dimensions, False otherwise.
    """
    for i in range(len(raster_paths) - 1):
        if not _compare_raster_dimensions(raster_paths[i], raster_paths[i + 1]):
            return False
    return True