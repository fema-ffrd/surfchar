"""Utility functions for the surfchar package."""

from osgeo import gdal
import pandas as pd

import shutil
import subprocess
from pathlib import Path
from typing import List

def get_cell_area(raster_path: str) -> float:
    ds = gdal.Open(raster_path)
    if ds is None:
        raise ValueError(f"Could not open raster: {raster_path}")

    gt = ds.GetGeoTransform()
    cell_area = abs(gt[1] * gt[5])

    ds = None
    return float(cell_area)

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


def raster_grid_args(template_raster_path: str) -> list[str]:
    """
    Return -te and -tr args matching a template raster.
    """

    ds = gdal.Open(template_raster_path)
    if ds is None:
        raise ValueError(
            f"Could not open template raster: {template_raster_path}"
        )
        
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
