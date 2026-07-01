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
    
    logger.info("Mapping filtered sinks...")

    filtered_sinks_path = os.path.join(interim_outputs_dir, "sinks.tif")

    raster.filter_sinks_raster(
        labeled_sinks_path=labeled_sinks_path,
        filtered_sinks_path=filtered_sinks_path,
        sink_ids=sinks_df["id"],
    )
    
    logger.info("Finding sink outlets...")
    logger.info('Getting outlet coordinates...')
    sinks_df["outlet_rc"] = raster.get_outlet_coords(
        labeled_sinks_path,
        flow_accumulation_path,
        sinks_df,
    )
    ds = gdal.Open(hydro_enforced_dem)

    gt = ds.GetGeoTransform()

    x_min = gt[0]
    y_max = gt[3]
    cell_width = gt[1]
    cell_height = abs(gt[5])

    ds = None
    sinks_df["outlet_xy"] = sinks_df["outlet_rc"].apply(
    raster.row_col_to_x_y,
    args=(
        x_min,
        y_max,
        cell_width,
        cell_height,
    ),
)
    
    outlets_shapefile_path = os.path.join(interim_outputs_dir, "outlets.shp")

    logger.info(f"Writing outlet points shapefile ({outlets_shapefile_path})...")

    raster.write_outlets_shapefile(
        sinks_df=sinks_df,
        shapefile_path=outlets_shapefile_path,
        template_raster_path=hydro_enforced_dem,
        sink_id_field="sink_id",
    )
    
    logger.info('Running watersheds...')
    watersheds_path = os.path.join(
        interim_outputs_dir,
        "watersheds.tif"
    )

    # Overflow bug here
    overflow.basins(
        fdr_path=flow_direction_path,
        drainage_points_path=outlets_shapefile_path,
        output_path=watersheds_path,
    )
    
    # watersheds_filled_polygons_buffer = None

    # if options.analyze_stage_storage:
    #     logger.info("Extracting watershed DEMs for rainfall volume analysis...")

    #     watershed_dems = raster.get_watershed_dems(
    #         hydro_dem_path=hydro_enforced_dem,
    #         watersheds_path=watersheds_path,
    #         sink_ids=sinks_df["id"],
    #     )

    #     logger.info("Calculating watershed fill elevations...")

    #     fill_elevs = raster.get_watershed_fill_elevs(
    #         watershed_dems=watershed_dems,
    #         sinks_df=sinks_df,
    #         rainfall_ft=options.excess_rainfall / 12.0,
    #         cell_area=cell_area,
    #     )

    #     sinks_df["fill_elev"] = fill_elevs

    #     logger.info("Mapping watershed fill elevations...")

    #     watersheds_filled_path = os.path.join(
    #         interim_outputs_dir,
    #         "watersheds_filled.tif",
    #     )

    #     raster.map_watershed_fill_raster(
    #         watersheds_path=watersheds_path,
    #         hydro_dem_path=hydro_enforced_dem,
    #         sinks_df=sinks_df,
    #         output_path=watersheds_filled_path,
    #     )


