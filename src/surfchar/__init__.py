"""surfchar: A Python package for characterizing surface water features."""
import os
import overflow
from . import raster
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

__version__ = "0.1.0"

def surfchar(hydro_enforced_dem: str, output_dir: str = "surfchar-output"):
    """Run the surfchar analysis."""
    logger.info("Running surfchar...")
    
    # Create output directory
    logger.info(f"Creating output directory: {output_dir}")
    os.mkdir(output_dir)
    interim_outputs_dir = os.path.join(output_dir, 'interim-outputs')
    os.mkdir(interim_outputs_dir)

    # Create filled hydro-enforced DEM
    filled_path = os.path.join(interim_outputs_dir, 'filled_dem.tif')
    logger.info(f"Creating filled hydro-enforced DEM ({filled_path})...")
    overflow.fill(hydro_enforced_dem, filled_path, working_dir=interim_outputs_dir)

    # Calculate sink depths
    sink_depths_path = os.path.join(interim_outputs_dir, 'sink_depths.tif')
    logger.info(f"Calculating sink depths ({sink_depths_path})...")
    raster.get_sink_depths(filled_path, hydro_enforced_dem, sink_depths_path)


