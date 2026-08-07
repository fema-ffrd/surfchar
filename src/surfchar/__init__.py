"""surfchar: A Python package for characterizing surface water features."""

from . import raster
from . import sinks
from surfchar.options import SurfcharOptions

from osgeo import gdal, ogr
import overflow

import os
import logging
from time import time, strftime
import pandas as pd

from . import raster
from . import sinks
from . import utils
from . import breaklines
from . import watersheds


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
    start = time()
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
    sinks.get_sink_depths(filled_path, hydro_enforced_dem, sink_depths_path)

    # Check sink depth stats
    logger.info(f"Checking sink depth stats for {sink_depths_path}...")
    min_sink_depth, max_sink_depth = sinks.check_sink_stats(sink_depths_path)
    logger.info(f"Sink depth stats - Min: {min_sink_depth}, Max: {max_sink_depth}")
    if min_sink_depth == max_sink_depth:
        error_msg = "All sink depths are the same. Check your input DEM. Don't use a filled DEM as input."
        logger.error(error_msg)
        raise NoSinksError(error_msg)

    # Label sinks
    labeled_sinks_path = os.path.join(interim_outputs_dir, "labeled_sinks.tif")
    logger.info(f"Labeling sinks ({labeled_sinks_path})...")
    num_sinks = sinks.label_sinks(sink_depths_path, labeled_sinks_path)
    logger.info(f"Number of sinks labeled: {num_sinks}")

    # Sink depth statistics
    sink_stats_df = sinks.get_sink_stats(labeled_sinks_path, sink_depths_path)
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
    
    flow_accumulation_int32_path = os.path.join(
        interim_outputs_dir,
        "flowacc_int32.tif",
    )

    utils.remove_output(flow_accumulation_int32_path)

    utils.run_cmd(
        [
            "gdal_translate",
            "-ot",
            "Int32",
            *raster.gdal_creation_option_args(),
            flow_accumulation_path,
            flow_accumulation_int32_path,
        ]
    )

    # Get flow accumulation statistics for each sink
    logger.info("Getting flow accumulation statistics for each sink...")
    flow_accum_stats_df = sinks.get_flowacc_stats(
        labeled_sinks_path, flow_accumulation_int32_path
    )
    logger.info(flow_accum_stats_df.head())

    # Build sinks DataFrame
    logger.info("Building sinks DataFrame...")
    # Open the original hydro enforced DEM to get the geotransform and projection
    cell_area = utils.get_cell_area(hydro_enforced_dem)
    sinks_df = sinks.build_sinks_df(
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
    sinks_filtered_path = os.path.splitext(labeled_sinks_path)[0] + "_filtered.gpkg"
    selected_sinks_path = os.path.join(interim_outputs_dir,"sinks_selected.gpkg")
    
    sinks.filter_sinks_vector_and_raster(
        sinks_filtered_path=sinks_filtered_path,
        selected_sinks_path=selected_sinks_path,
        filtered_sinks_path=filtered_sinks_path,
        template_raster_path=labeled_sinks_path,
        sink_ids=sinks_df["id"],
    )
    
    logger.info("Finding sink outlets...")
    logger.info('Getting outlet coordinates...')
    sinks_df["outlet_xy"] = watersheds.get_outlet_coords(
        labeled_sinks_path,
        flow_accumulation_int32_path,
        sinks_df,
    )
    
    outlets_path = os.path.join(interim_outputs_dir, "outlets.gpkg")

    logger.info(f"Writing outlet points GeoPackage ({outlets_path})...")

    watersheds.write_outlets_gpkg(
        sinks_df=sinks_df,
        gpkg_path=outlets_path,
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
        drainage_points_path=outlets_path,
        output_path=watersheds_path,
    )
    
    logger.info("Mapping sink IDs to watershed IDs...")

    sinks_df["watershed_id"] = sinks_df["outlet_xy"].apply(
        lambda p: watersheds.get_watershed_id_at_outlet(
            watersheds_path=watersheds_path,
            x=p[0],
            y=p[1],
        )
    )

    logger.info(sinks_df[["id", "watershed_id"]].head())
    
    watersheds_filled_polygons_buffer = None
    
    
    if options.analyze_stage_storage:
        logger.info("Building stage-storage tables...")
        
        stage_storage_dir = os.path.join(
        interim_outputs_dir,
        "stage_storage",
    )

        os.makedirs(stage_storage_dir, exist_ok=True)

        stage_storage_df = raster.get_stage_storage_table_gdal(
            watersheds_path=watersheds_path,
            hydro_dem_path=hydro_enforced_dem,
            sink_ids=sinks_df["watershed_id"],
            output_dir=stage_storage_dir,
            stage_step = 15,
            # stage_step=0.25,
        )

        sinks_df = raster.get_fill_elevs_from_stage_storage(
            stage_storage_df=stage_storage_df,
            sinks_df=sinks_df,
            rainfall_ft=options.excess_rainfall / 12.0,
        )

        logger.info("Mapping watershed fill elevations...")

        watersheds_filled_path = os.path.join(
            interim_outputs_dir,
            "watersheds_filled.tif",
        )
        
        fill_elev_csv = os.path.join(
            stage_storage_dir,
            "fill_elev.csv",
        )

        raster.write_fill_elev_csv(
            sinks_df=sinks_df,
            csv_path=fill_elev_csv,
        )
        watersheds_vector_path = os.path.join(
            stage_storage_dir,
            "watersheds.gpkg",
        )

        raster.raster_to_polygons(
            raster_path=watersheds_path,
            output_path=watersheds_vector_path,
        )
        watersheds_fill_elev_vector_path = os.path.join(
            stage_storage_dir,
            "watersheds_fill_elev.gpkg",
        )

        raster.join_fill_elev_to_watersheds(
            watersheds_vector_path=watersheds_vector_path,
            fill_elev_csv=fill_elev_csv,
            output_vector_path=watersheds_fill_elev_vector_path,
        )
        
        raster.map_watershed_fill_raster_gdal(
            hydro_dem_path=hydro_enforced_dem,
            watersheds_id_raster_path=watersheds_path,
            watersheds_fill_elev_vector_path=watersheds_fill_elev_vector_path,
            output_path=watersheds_filled_path,
            output_dir=stage_storage_dir,
        )
        logger.info("Converting watershed fill raster to polygons...")
        
        watersheds_filled_polygons = os.path.join(
            interim_outputs_dir,
            "watersheds_filled_polygons.shp",
        )

        raster.raster_to_polygons(
            raster_path=watersheds_filled_path,
            output_path=watersheds_filled_polygons,
        )

        logger.info(f"Buffering watershed fill polygons by {options.sink_buffer} ft...")

        watersheds_filled_polygons_buffer = os.path.join(
            interim_outputs_dir,
            "watersheds_filled_polygons_buffer.shp",
        )

        breaklines.buffer_polygons(
            input_path=watersheds_filled_polygons,
            output_path=watersheds_filled_polygons_buffer,
            distance=options.sink_buffer,
        )
        
    sinks_csv_path = os.path.join(interim_outputs_dir, 'sinks.csv')
    logger.info(f'Writing sinks data to {sinks_csv_path}')
    sinks_df.to_csv(sinks_csv_path, index=False)
    
    sinks_polygons = os.path.join(interim_outputs_dir, 'sinks_polygons.shp')
    logger.info(f'Converting sinks raster to polygons {sinks_polygons}')
    raster.raster_to_polygons(raster_path=filtered_sinks_path, output_path=sinks_polygons,)
    
    
    watersheds_polygons = os.path.join(interim_outputs_dir, "watersheds_polygons.shp",)
    logger.info(f'Converting watersheds raster to polygons {watersheds_polygons}')
    raster.raster_to_polygons(raster_path=watersheds_path, output_path=watersheds_polygons,)
    
    watersheds_lines = os.path.join(interim_outputs_dir, 'watersheds_lines.shp')
    logger.info(f'Converting watershed polygons to lines {watersheds_lines}')
    breaklines.polygons_to_lines(watersheds_polygons, watersheds_lines, )

    sinks_polygons_buffer = os.path.join(interim_outputs_dir, 'sinks_polygons_buffer.shp')
    logger.info(f'Buffering sinks by {options.sink_buffer} ft...')
    breaklines.buffer_polygons(sinks_polygons, sinks_polygons_buffer, options.sink_buffer)
    
    watersheds_lines_clipped = os.path.join(interim_outputs_dir,"watersheds_lines_clipped.shp",)
    logger.info(f"Clipping watershed lines {watersheds_lines_clipped}")
    
    #debug
    print(sinks_df[["id", "watershed_id", "overflows"]])
    
    print("Overflow count:", sinks_df["overflows"].sum())

    # print(sinks_df[["id", "fill_elev"]])
    
    # ArcPy logic:
    # overflow status belongs to the sink, so retain sink IDs here.
    sinks_that_overflow = sinks_df.loc[
        sinks_df["overflows"].fillna(False).astype(bool),
        "id",
    ]

    # The ArcPy watershed raster used sink IDs directly.
    # The GDAL watershed raster currently has different watershed values,
    # so preserve the relationship explicitly.
    watershed_to_sink = {
        int(row.watershed_id): int(row.id)
        for row in sinks_df[["id", "watershed_id"]].itertuples(index=False)
        if pd.notna(row.watershed_id)
    }

    logger.info(
        "Overflow sink IDs: %s",
        sinks_that_overflow.astype(int).tolist(),
    )

    logger.info(
        "Watershed-to-sink mapping: %s",
        watershed_to_sink,
    )

    breaklines.clip_watersheds(
        watersheds=watersheds_lines,
        sinks_buffer=sinks_polygons_buffer,
        sinks_that_overflow=sinks_that_overflow,
        watersheds_clipped=watersheds_lines_clipped,
        watershed_to_sink=watershed_to_sink,
        watersheds_fill_buffer=watersheds_filled_polygons_buffer,
        clip_all=options.clip_all,
        min_breakline_length=options.min_breakline_length,
    )
    
    breaklines_path = os.path.join(output_dir,"breaklines.shp")

    logger.info(f"Dissolving clipped watershed lines to get initial breakline geometry {breaklines_path}")

    initial_breaklines = breaklines.dissolve_breaklines(watersheds_lines_clipped)
                                                    
    logger.info(f'Fixing self-closing breaklines...')
    fixed_breaklines = breaklines.fix_self_closing_breaklines(initial_breaklines)

    logger.info(f'Writing breaklines shapefile {breaklines_path}')
    ds = ogr.Open(watersheds_lines_clipped)

    layer = ds.GetLayer()

    srs = layer.GetSpatialRef().Clone()

    ds = None

    breaklines.write_breaklines_shapefile(
        fixed_breaklines,
        breaklines_path,
        srs=srs,
    )

    logger.info(f'Done. Elapsed time: {time() - start:.2f} seconds')
    




