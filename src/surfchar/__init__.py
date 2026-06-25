"""surfchar: A Python package for characterizing surface water features."""

from . import raster
from . import sinks
from surfchar.options import SurfcharOptions

from osgeo import gdal
import overflow

import os
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

__version__ = "0.1.0"


class NoSinksError(Exception):
    """Custom exception raised when no sinks are found in the DEM."""

    pass


def surfchar(
    hydro_enforced_dem: str,
    output_dir: str = "surfchar-output",
    options: SurfcharOptions = SurfcharOptions(),
):
    """Run the surfchar analysis."""
    logger.info("Running surfchar...")

    # Create output directory
    logger.info(f"Creating output directory: {output_dir}")
    os.mkdir(output_dir)
    interim_outputs_dir = os.path.join(output_dir, "interim-outputs")
    os.mkdir(interim_outputs_dir)

    # Create filled hydro-enforced DEM
    filled_path = os.path.join(interim_outputs_dir, "filled_dem.tif")
    logger.info(f"Creating filled hydro-enforced DEM ({filled_path})...")
    overflow.fill(hydro_enforced_dem, filled_path, working_dir=interim_outputs_dir)

    # Calculate sink depths
    sink_depths_path = os.path.join(interim_outputs_dir, "sink_depths.tif")
    logger.info(f"Calculating sink depths ({sink_depths_path})...")
    raster.get_sink_depths(filled_path, hydro_enforced_dem, sink_depths_path)

    # Check sink depth stats
    logger.info(f"Checking sink depth stats for {sink_depths_path}...")
    min_sink_depth, max_sink_depth = raster.check_sink_stats(sink_depths_path)
    logger.info(f"Sink depth stats - Min: {min_sink_depth}, Max: {max_sink_depth}")
    if min_sink_depth == max_sink_depth:
        error_msg = "All sink depths are the same. Check your input DEM. Don't use a filled DEM as input."
        logger.error(error_msg)
        raise NoSinksError(error_msg)

    # Label sinks
    labeled_sinks_path = os.path.join(interim_outputs_dir, "labeled_sinks.tif")
    logger.info(f"Labeling sinks ({labeled_sinks_path})...")
    num_sinks = raster.label_sinks(sink_depths_path, labeled_sinks_path)
    logger.info(f"Number of sinks labeled: {num_sinks}")

    # Sink depth statistics
    print("Getting sink depth statistics...")
    sink_stats_df = raster.get_sink_stats(labeled_sinks_path, sink_depths_path)
    logger.info(sink_stats_df.head())

    # Compute flow direction from the filled DEM
    flow_direction_path = os.path.join(interim_outputs_dir, "flowdir.tif")
    logger.info(f"Calculating flow direction ({flow_direction_path})...")
    overflow.flow_direction(
        filled_path, flow_direction_path, working_dir=interim_outputs_dir
    )

    # Run flow accumulation from the flow direction raster
    flow_accumulation_path = os.path.join(interim_outputs_dir, "flowacc.tif")
    logger.info(f"Calculating flow accumulation ({flow_accumulation_path})...")
    overflow.accumulation(flow_direction_path, flow_accumulation_path)

    # Get flow accumulation statistics for each sink
    logger.info("Getting flow accumulation statistics for each sink...")
    flow_accum_stats_df = raster.get_flowacc_stats(
        labeled_sinks_path, flow_accumulation_path
    )
    logger.info(flow_accum_stats_df.head())

    # Build sinks DataFrame
    logger.info("Building sinks DataFrame...")
    # Open the original hydro enforced DEM to get the geotransform and projection
    with gdal.Open(hydro_enforced_dem) as ds:
        gt = ds.GetGeoTransform()
        cell_area = abs(gt[1] * gt[5])
    sinks_df = raster.build_sinks_df(
        sink_stats_df, flow_accum_stats_df, cell_area, rainfall_inches=1.0
    )
    logger.info(f"{len(sinks_df)} total sinks.")
    logger.info(sinks_df.head())

    # Write full sinks DataFrame to CSV
    sinks_csv_path = os.path.join(output_dir, "sinks.csv")
    logger.info(f"Writing sinks DataFrame to CSV ({sinks_csv_path})...")
    sinks_df.to_csv(sinks_csv_path, index=False)

    # Filter sinks based on options
    logger.info("Filtering sinks based on options...")
    logger.info(f"Minimum sink area: {options.filter_min_area} sq ft")
    logger.info(f"Minimum sink depth: {options.filter_min_avg_depth} ft")
    logger.info(f"Minimum sink volume: {options.filter_min_vol} acre-ft")
    logger.info(
        f"Minimum sink contributing drainage area: {options.filter_min_cda} sq mi"
    )
    logger.info(
        f"Minimum sink volume to contributing drainage area ratio: {options.filter_min_vol_cda_ratio}"
    )
    sinks_df = sinks.filter_sinks_df(sinks_df, options)
    logger.info(f"{len(sinks_df)} sinks remaining after filtering.")
    logger.info(sinks_df.head())
