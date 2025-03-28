import pandas as pd
import numpy as np

def generate_pattern(segments, time_index):
    vals = []
    for val, count in segments:
        vals.extend([val] * count)

    # Repeat the pattern if time_index is longer than the segments
    while len(vals) < len(time_index):
        vals.extend(vals)

    return pd.DataFrame({"value": vals[:len(time_index)]}, index=time_index)

###############################################################################
# Residential-like pattern
###############################################################################
def create_residential_like_pattern(time_index):
    segments = [
        (0.05, 6 * 4),  # 0:00–6:00
        (0.20, 2 * 4),  # 6:00–8:00
        (0.35, 8 * 4),  # 8:00–16:00
        (0.45, 4 * 4),  # 16:00–20:00
        (0.15, 4 * 4),  # 20:00–24:00
    ]
    return generate_pattern(segments, time_index)

###############################################################################
# Office-like pattern
###############################################################################
def create_office_like_pattern(time_index):
    segments = [
        (0.01, 7 * 4),  # 0:00–7:00
        (0.25, 1 * 4),  # 7:00–8:00
        (0.40, 9 * 4),  # 8:00–17:00
        (0.30, 3 * 4),  # 17:00–20:00
        (0.10, 4 * 4),  # 20:00–24:00
    ]
    return generate_pattern(segments, time_index)

###############################################################################
# Commercial-like pattern 1
###############################################################################
def create_commercial_like_pattern1(time_index):
    # Bidder C: Commercial-like pattern
    segments = [
        (0.10, 6 * 4),  # 0:00–6:00
        (0.20, 6 * 4),  # 6:00–12:00
        (0.45, 6 * 4),  # 12:00–18:00
        (0.30, 6 * 4),  # 18:00–24:00
    ]
    return generate_pattern(segments, time_index)

###############################################################################
# Commercial-like pattern 2
###############################################################################
def create_commercial_like_pattern2(time_index):
    segments = [
        (0.05, 6 * 4),
        (0.20, 2 * 4),
        (0.40, 8 * 4),
        (0.35, 4 * 4),
        (0.15, 4 * 4),
    ]
    return generate_pattern(segments, time_index)

###############################################################################
# Battery that can discharge (negative load)
###############################################################################
def create_battery_pattern(time_index):
    segments = [
        (0.30, 4 * 4),  # 0:00–4:00 charging
        (-0.20, 2 * 4),  # 4:00–6:00 discharging
        (0.25, 10 * 4),  # 6:00–16:00 charging
        (-0.15, 4 * 4),  # 16:00–20:00 discharging
        (0.10, 4 * 4),  # 20:00–24:00 light charging
    ]
    return generate_pattern(segments, time_index)


