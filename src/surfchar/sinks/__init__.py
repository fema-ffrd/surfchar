"""surfchar.sinks: Functions for filtering and analyzing sinks."""

from surfchar.options import SurfcharOptions

import pandas as pd


def filter_sinks_df(sinks_df: pd.DataFrame, options: SurfcharOptions) -> pd.DataFrame:
    """
    Filter sinks based on the given filter parameters.

    Parameters
    ----------
    sinks_df : pd.DataFrame
        DataFrame containing sink information.
    options : SurfcharOptions
        Options containing filter parameters.

    Returns
    -------
    pd.DataFrame
        Filtered DataFrame containing sinks that meet the criteria.

    """
    return sinks_df.loc[
        (sinks_df["area"] > options.filter_min_area)
        & (sinks_df["avg_depth"] > options.filter_min_avg_depth)
        & (
            sinks_df["vol"] > options.filter_min_vol * 43560
        )  # convert acre-ft to cubic feet
        & (
            sinks_df["cda"] > options.filter_min_cda * 5280**2
        )  # convert square miles to square feet
        & (sinks_df["vol_cda_ratio"] > options.filter_min_vol_cda_ratio)
    ]
