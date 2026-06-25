"""CLI for surfchar."""

from argparse import ArgumentParser
from surfchar import __version__, surfchar
from surfchar.options import *


def main():
    """Initialize the surfchar CLI."""
    parser = ArgumentParser()
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s " + __version__,
    )
    parser.add_argument(
        "--hydro-enforced-dem",
        type=str,
        required=True,
        help="Path to the hydro-enforced DEM raster file.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="surfchar-output",
        help="Directory to save the output files (default: surfchar-output).",
    )
    parser.add_argument(
        "--min-sink-area",
        type=float,
        default=SurfcharOptions.filter_min_area,
        help=f"Minimum sink area in square feet (default: {SurfcharOptions.filter_min_area}).",
    )
    parser.add_argument(
        "--min-sink-depth",
        type=float,
        default=SurfcharOptions.filter_min_avg_depth,
        help=f"Minimum sink depth in feet (default: {SurfcharOptions.filter_min_avg_depth}).",
    )
    parser.add_argument(
        "--min-sink-volume",
        type=float,
        default=SurfcharOptions.filter_min_vol,
        help=f"Minimum sink volume in acre-feet (default: {SurfcharOptions.filter_min_vol}).",
    )
    parser.add_argument(
        "--min-sink-cda",
        type=float,
        default=SurfcharOptions.filter_min_cda,
        help=f"Minimum sink contributing drainage area in square miles (default: {SurfcharOptions.filter_min_cda}).",
    )
    parser.add_argument(
        "--min-sink-volume-cda-ratio",
        type=float,
        default=SurfcharOptions.filter_min_vol_cda_ratio,
        help=f"Minimum sink volume to contributing drainage area ratio (default: {SurfcharOptions.filter_min_vol_cda_ratio}).",
    )
    parser.add_argument(
        "--excess-rainfall",
        type=float,
        default=SurfcharOptions.excess_rainfall,
        help=f"Excess rainfall in inches (default: {SurfcharOptions.excess_rainfall}).",
    )
    parser.add_argument(
        "--analyze-stage-storage",
        action="store_true",
        help="Analyze stage-storage relationships.",
    )
    parser.add_argument(
        "--clip-all",
        action="store_true",
        help="Clip all sink watershed boundaries to form breaklines, including sinks that don't overflow.",
    )
    parser.add_argument(
        "--sink-buffer",
        type=float,
        default=SurfcharOptions.sink_buffer,
        help=f"Sink buffer in feet (default: {SurfcharOptions.sink_buffer}).",
    )
    parser.add_argument(
        "--min-breakline-length",
        type=float,
        default=SurfcharOptions.min_breakline_length,
        help=f"Minimum breakline length in feet (default: {SurfcharOptions.min_breakline_length}).",
    )
    args = parser.parse_args()
    options = SurfcharOptions(
        filter_min_area=args.min_sink_area,
        filter_min_avg_depth=args.min_sink_depth,
        filter_min_vol=args.min_sink_volume,
        filter_min_cda=args.min_sink_cda,
        filter_min_vol_cda_ratio=args.min_sink_volume_cda_ratio,
        excess_rainfall=args.excess_rainfall,
        analyze_stage_storage=args.analyze_stage_storage,
        clip_all=args.clip_all,
        sink_buffer=args.sink_buffer,
        min_breakline_length=args.min_breakline_length,
    )
    surfchar(args.hydro_enforced_dem, args.output_dir, options)


if __name__ == "__main__":
    main()
