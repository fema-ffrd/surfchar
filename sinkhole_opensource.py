# import arcpy
# import dask.array as da
# from dask import delayed
# from dask.distributed import Client, LocalCluster
# import numpy as np
# from osgeo import gdal, osr
# import pandas as pd
# from scipy.optimize import minimize_scalar
# import xarray as xr

# import argparse
# import logging
import os
from time import time, strftime
# from typing import Generator, List, NamedTuple, Optional, Tuple, Union
from osgeo import gdal
import overflow

gdal.UseExceptions()

def raster_sizes_match(hydro_dem_path: str, flowacc_path: str, flowdir_path: str) -> bool:
    """
    Determine if the Hydro DEM, Flow Accumulation, and Flow Direction rasters
    have the same width (columns) and height (rows).
    """
    hydro_dem = None
    flowacc = None
    flowdir = None

    try:
        hydro_dem = gdal.Open(hydro_dem_path)
        flowacc = gdal.Open(flowacc_path)
        flowdir = gdal.Open(flowdir_path)

        if not hydro_dem or not flowacc or not flowdir:
            raise ValueError("One or more raster files could not be opened.")

        return all(
            ds.RasterXSize == hydro_dem.RasterXSize and
            ds.RasterYSize == hydro_dem.RasterYSize
            for ds in [flowacc, flowdir]
        )
    finally:
        hydro_dem = None
        flowacc = None
        flowdir = None

    
def analyze(hydro_dem_path: str, flowacc_path: str, flowdir_path: str, output_dir_path: str, options: dict,
            arc_toolbox=False):
    start = time()

    print(f'Hydro DEM: {hydro_dem_path}')
    print(f'Flow accumulation: {flowacc_path}')
    print(f'Flow direction: {flowdir_path}')
    print(f'Output directory: {output_dir_path}')
    print(f'Options: {options}')

    if not raster_sizes_match(hydro_dem_path, flowacc_path, flowdir_path):
        err = (
            'Hydro DEM, Flow Accumulation, and Flow Direction raster width (cols) and height (rows) must '
            'match. Please clip your rasters!'
        )
        print(err)

    print(f'Creating output directory {output_dir_path}')
    os.mkdir(output_dir_path)
    interim_outputs_dir = os.path.join(output_dir_path, 'interim-outputs')
    os.mkdir(interim_outputs_dir)
    
    filled_path = os.path.join(output_dir_path, "filled.tif")
    print("Filling hydro DEM...")
    overflow.fill(hydro_dem_path, filled_path, working_dir = output_dir_path)


# for testing 

if __name__ == "__main__":
    hydro_dem = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/dem_2.tif"
    flowacc = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/facc_2.tif"
    flowdir = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/fdir_2.tif"
    output_path = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/Output2026"

    result = raster_sizes_match(hydro_dem, flowacc, flowdir)
    print(f"Do raster sizes match? {result}")
    
    analyze(hydro_dem, flowacc, flowdir, output_path, dict)