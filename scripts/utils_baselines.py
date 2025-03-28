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

def create_duck_curve_pattern(time_index, num_days):
    """
    Create a duck curve pattern for the given time index and number of days.

    :param time_index: Pandas DatetimeIndex for the time slots
    :param num_days: Number of days to repeat the pattern
    :return: Pandas DataFrame with the duck curve pattern
    """
    duck_base = np.array([
        0.4, 0.3, 0.25, 0.25, 0.3, 0.3, 0.2, 0.2, 0.2, 0.2, 0.2, 0.25,
        0.3, 0.3, 0.35, 0.4, 0.45, 0.5, 0.6, 0.7, 0.8, 0.8, 0.8, 0.7,
        0.6, 0.5, 0.4, 0.35, 0.3, 0.3, 0.3, 0.25, 0.2, 0.15, 0.1, 0.1,
        0.1, 0.1, 0.2, 0.3, 0.4, 0.4, 0.5, 0.55, 0.6, 0.7, 0.8, 0.85,
        0.9, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 0.9, 0.9, 0.8, 0.8,
        0.7, 0.7, 0.7, 0.6, 0.5, 0.4, 0.4, 0.35, 0.3, 0.2, 0.2, 0.2,
        0.25, 0.3, 0.3, 0.4, 0.5, 0.7, 0.9, 1.0, 1.1, 1.2, 1.2, 1.1,
        1.0, 0.9, 0.8, 0.7, 0.6, 0.5, 0.5, 0.4, 0.3, 0.2, 0.2, 0.2
    ])
    duck_base = np.tile(duck_base, num_days)
    df_duck = pd.DataFrame({'demand': duck_base}, index=time_index)
    return df_duck

def create_bus_curve_pattern(time_index, num_days):
    """
    Create a bus curve pattern for the given time index and number of days.

    :param time_index: Pandas DatetimeIndex for the time slots
    :param num_days: Number of days to repeat the pattern
    :return: Pandas DataFrame with the bus curve pattern
    """
    bus_base = np.array([
        0.0, 0.0, 0.0, 0.2, 0.5, 0.5, 0.5, 0.5, 0.3, 0.3, 0.3, 0.2,
        0.2, 0.2, 0.3, 0.4, 0.5, 1.0, 1.2, 1.2, 1.2, 1.2, 1.2, 1.0,
        0.8, 0.6, 0.3, 0.3, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.5, 0.5,
        1.0, 1.5, 1.5, 1.5, 1.5, 1.2, 1.0, 0.8, 0.8, 0.8, 0.8, 0.8,
        0.8, 0.8, 0.8, 0.8, 1.0, 1.0, 1.0, 1.0, 1.2, 1.2, 1.2, 1.0,
        0.8, 0.6, 0.6, 0.5, 0.4, 0.4, 0.4, 0.4, 0.4, 0.4, 0.8, 1.0,
        1.0, 1.2, 1.2, 1.2, 1.2, 1.0, 0.8, 0.6, 0.5, 0.5, 0.4, 0.3,
        0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2, 0.4, 0.6, 0.8, 1.0
    ])
    bus_base = np.tile(bus_base, num_days)
    df_bus = pd.DataFrame({'demand': bus_base}, index=time_index)
    return df_bus