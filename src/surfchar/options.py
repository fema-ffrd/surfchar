"""Default parameters for surfchar analysis."""

from dataclasses import dataclass

FILTER_MIN_AREA = 2500.0  # ft**2
FILTER_MIN_AVG_DEPTH = 1.0  # ft
FILTER_MIN_VOL = 10.0  # ac-ft
FILTER_MIN_CDA = 0.1  # mi**2
FILTER_MIN_VOL_CDA_RATIO = 0.1
EXCESS_RAINFALL = 6.0  # in
ANALYZE_STAGE_STORAGE = False
CLIP_ALL = False
SINK_BUFFER = 400  # ft
MIN_BREAKLINE_LENGTH = 200  # ft


@dataclass
class SurfcharOptions:
    filter_min_area: float = FILTER_MIN_AREA
    filter_min_avg_depth: float = FILTER_MIN_AVG_DEPTH
    filter_min_vol: float = FILTER_MIN_VOL
    filter_min_cda: float = FILTER_MIN_CDA
    filter_min_vol_cda_ratio: float = FILTER_MIN_VOL_CDA_RATIO
    excess_rainfall: float = EXCESS_RAINFALL
    analyze_stage_storage: bool = ANALYZE_STAGE_STORAGE
    clip_all: bool = CLIP_ALL
    sink_buffer: float = SINK_BUFFER
    min_breakline_length: float = MIN_BREAKLINE_LENGTH
