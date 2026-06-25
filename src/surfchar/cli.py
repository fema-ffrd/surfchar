"""CLI for surfchar."""

from argparse import ArgumentParser
from surfchar import __version__, surfchar
from surfchar.defaults import *

def main():
    """Initialize the surfchar CLI."""
    parser = ArgumentParser()
    parser.add_argument(
        "--hydro-enforced-dem",
        type=str,
        required=True,
        help="Path to the hydro-enforced DEM raster file.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + __version__,
    )
    args = parser.parse_args()
    surfchar(hydro_enforced_dem=args.hydro_enforced_dem)

if __name__ == "__main__":
    main()
