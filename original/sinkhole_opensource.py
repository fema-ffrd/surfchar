# import arcpy
# import dask.array as da
# from dask import delayed
# from dask.distributed import Client, LocalCluster
import numpy as np
# from osgeo import gdal, osr
import pandas as pd
# from scipy.optimize import minimize_scalar
# import xarray as xr

# import argparse
# import logging
import os
from time import time, strftime
# from typing import Generator, List, NamedTuple, Optional, Tuple, Union
from osgeo import gdal
from osgeo_utils import gdal_calc
import overflow

from scipy import ndimage


# Parameter names and defaults
PARAM_FILTER_MIN_AREA = 'filter_min_area'
PARAM_FILTER_MIN_AVG_DEPTH = 'filter_min_avg_depth'
PARAM_FILTER_MIN_VOL = 'filter_min_vol'
PARAM_FILTER_MIN_CDA = 'filter_min_cda'
PARAM_FILTER_MIN_VOL_CDA_RATIO = 'filter_min_vol_cda_ratio'
PARAM_EXCESS_RAINFALL = 'excess_rainfall'
PARAM_ANALYZE_STAGE_STORAGE = 'analyze_stage_storage'
PARAM_CLIP_ALL = 'clip_all'
PARAM_SINK_BUFFER = 'sink_buffer'
PARAM_MIN_BREAKLINE_LENGTH = 'min_breakline_length'
PARAM_CHUNK_WIDTH = 'chunk_width'
PARAM_CHUNK_HEIGHT = 'chunk_height'
PARAM_DASK_N_WORKERS = 'dask_n_workers'
PARAM_DEFAULTS = {
    PARAM_FILTER_MIN_AREA: 2500.0,  # ft**2
    PARAM_FILTER_MIN_AVG_DEPTH: 1.0,  # ft
    PARAM_FILTER_MIN_VOL: 10.0,  # ac-ft
    PARAM_FILTER_MIN_CDA: 0.1,  # mi**2
    PARAM_FILTER_MIN_VOL_CDA_RATIO: 0.1,
    PARAM_EXCESS_RAINFALL: 6.0,  # in
    PARAM_ANALYZE_STAGE_STORAGE: False,
    PARAM_CLIP_ALL: False,
    PARAM_SINK_BUFFER: 400,  # ft
    PARAM_MIN_BREAKLINE_LENGTH: 200,  # ft
    PARAM_CHUNK_WIDTH: 4096,
    PARAM_CHUNK_HEIGHT: 4096,
    PARAM_DASK_N_WORKERS: 8 if os.cpu_count() > 8 else os.cpu_count() - 1
}

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
        


def get_sink_depths(filled_path, hydro_dem_path, output_path):
    gdal_calc.Calc(
        calc="A-B",
        A=filled_path,
        B=hydro_dem_path,
        outfile=output_path,
        type="Float32"
    )
    

def check_sink_stats(raster_path):
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
    Convert sink depths raster into an integer raster where each connected sink
    region has a unique ID (8-connectivity).

    Returns:
        num_features: number of labeled sink regions
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
    structure = np.ones((3, 3), dtype=np.uint8)

    labeled, num_features = ndimage.label(mask, structure=structure)

    driver = gdal.GetDriverByName("GTiff")
    out_ds = driver.Create(
        labeled_sinks_path,
        ds.RasterXSize,
        ds.RasterYSize,
        1,
        gdal.GDT_Int32
    )

    out_ds.SetGeoTransform(ds.GetGeoTransform())
    out_ds.SetProjection(ds.GetProjection())

    out_band = out_ds.GetRasterBand(1)
    out_band.WriteArray(labeled.astype(np.int32))
    out_band.SetNoDataValue(0)   # background = 0
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
        valid &= (labels != labels_nodata)
    if depths_nodata is not None:
        valid &= (depths != depths_nodata)

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

    sink_stats_df = pd.DataFrame({
        "VALUE": sink_ids.astype(np.int32),
        "COUNT": counts.astype(np.int32),
        "AREA": counts.astype(np.float64) * cell_area,
        "MIN": np.asarray(mins, dtype=np.float64),
        "MAX": np.asarray(maxs, dtype=np.float64),
        "MEAN": np.asarray(means, dtype=np.float64),
        "SUM": np.asarray(sums, dtype=np.float64),
    })

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
        valid &= (labels != labels_nodata)
    if flowacc_nodata is not None:
        valid &= (flowacc != flowacc_nodata)

    sink_ids = np.unique(labels[valid])
    sink_ids = sink_ids[sink_ids > 0]

    if sink_ids.size == 0:
        return pd.DataFrame(columns=["VALUE", "max_facc"])

    valid_labels = labels[valid]
    valid_flowacc = flowacc[valid]

    max_facc = ndimage.maximum(valid_flowacc, labels=valid_labels, index=sink_ids)

    facc_stats_df = pd.DataFrame({
        "VALUE": sink_ids.astype(np.int32),
        "max_facc": np.asarray(max_facc).astype(np.int32),
    })

    labels_ds = None
    flowacc_ds = None
    return facc_stats_df

def build_sinks_df(sink_stats_df: pd.DataFrame, flowacc_stats_df: pd.DataFrame, cell_area: float,
                   rainfall_inches: float) -> pd.DataFrame:
    """
    Given sink depth statistics, flow accumulation statistics, cell area, and rainfall amount,
    build the initial sinks DataFrame.
    """
    sinks_df = pd.DataFrame({
        'id': sink_stats_df['VALUE'],
        'count': sink_stats_df['COUNT'],
        'area': sink_stats_df['AREA'],
        'max_depth': sink_stats_df['MAX'],
        'avg_depth': sink_stats_df['MEAN'],
        'vol': sink_stats_df['SUM'] * cell_area,
    })
    sinks_df = sinks_df.join(flowacc_stats_df.set_index('VALUE'), on='id')
    sinks_df['cda'] = sinks_df['max_facc'] * cell_area
    sinks_df['vol_cda_ratio'] = sinks_df['vol'] / sinks_df['cda']
    sinks_df['filled_vol'] = sinks_df['cda'] * rainfall_inches / 12.0
    sinks_df['overflows'] = [True if sink.filled_vol > sink.vol else False for sink in sinks_df.itertuples()]
    return sinks_df

def filter_sinks_df(sinks_df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """
    Filter sinks based on the given filter parameters.
    """
    return sinks_df.loc[
        (sinks_df['area'] > filters[PARAM_FILTER_MIN_AREA])
        & (sinks_df['avg_depth'] > filters[PARAM_FILTER_MIN_AVG_DEPTH])
        & (sinks_df['vol'] > filters[PARAM_FILTER_MIN_VOL] * 43560)
        & (sinks_df['cda'] > filters[PARAM_FILTER_MIN_CDA] * 5280 ** 2)
        & (sinks_df['vol_cda_ratio'] > filters[PARAM_FILTER_MIN_VOL_CDA_RATIO])
    ]
    
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
    
    print('Getting sink depths...')
    sink_depths_path = os.path.join(interim_outputs_dir, "sink_depths.tif")
    get_sink_depths(filled_path, hydro_dem_path, sink_depths_path)
    print('Calculating sink depth raster statistics...')

    min_val, max_val = check_sink_stats(sink_depths_path)
    if min_val == max_val:
        raise Exception('No sinks found! Has your Hydro DEM been Filled?') 
    print(f'Sink depths raster saved: {sink_depths_path}')
    
    print('Labeling sinks...')
    labeled_sinks_path = os.path.join(interim_outputs_dir, 'all_sinks.tif')
    num_sinks = label_sinks(sink_depths_path, labeled_sinks_path)
    print(f'Saved labeled sinks raster {labeled_sinks_path}')
    print(f'Number of labeled sink regions: {num_sinks}')
    
    print('Getting sink depth statistics...')
    sink_stats_df = get_sink_stats(labeled_sinks_path, sink_depths_path)
    print(sink_stats_df.head())
    
    print('Getting flow accumulation statistics...')
    flowacc_stats_df = get_flowacc_stats(labeled_sinks_path, flowacc_path)
    print(flowacc_stats_df.head())
    
    print('Building sinks DataFrame...')
    # open the hydro dem and get cell size
    ds = gdal.Open(hydro_dem_path)
    gt = ds.GetGeoTransform()
    cell_area = abs(gt[1] * gt[5])
    ds = None
    sinks_df = build_sinks_df(sink_stats_df, flowacc_stats_df, cell_area, options[PARAM_EXCESS_RAINFALL])
    print(f'{len(sinks_df)} total sinks')
    print(sinks_df)
    
    all_sinks_csv_path = os.path.join(interim_outputs_dir, 'all_sinks.csv')
    print(f'Writing unfiltered sinks data to {all_sinks_csv_path}')
    sinks_df.to_csv(all_sinks_csv_path, index=False)
    
    print('Filtering sinks:')
    print(f'\tarea > {options[PARAM_FILTER_MIN_AREA]} ft^2')
    print(f'\tavg depth > {options[PARAM_FILTER_MIN_AVG_DEPTH]} ft')
    print(f'\tvolume > {options[PARAM_FILTER_MIN_VOL]} ac-ft')
    print(f'\tcontributing drainage area > {options[PARAM_FILTER_MIN_CDA]} mi^2')
    print(f'\tvolume / CDA ratio > {options[PARAM_FILTER_MIN_VOL_CDA_RATIO]}')
    sinks_df = filter_sinks_df(sinks_df, options)
    print(sinks_df)
    print(f'{len(sinks_df)} sinks remain after filtering')
    
    

# for testing 

if __name__ == "__main__":
    hydro_dem = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/dem_2.tif"
    flowacc = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/facc_2.tif"
    flowdir = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/fdir_2.tif"
    output_path = r"C:/Users/USQC714579/Documents/Sinkhole/test_data/small/Output2026"

    result = raster_sizes_match(hydro_dem, flowacc, flowdir)
    print(f"Do raster sizes match? {result}")
    
    analyze(hydro_dem, flowacc, flowdir, output_path, PARAM_DEFAULTS)