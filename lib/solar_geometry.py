"""
Solar position math -- pure functions, no I/O, no external astronomy
library. Everything here is degrees in / degrees out unless noted.

WHY WE NEED THIS
------------------
Shadow length and direction (lib/shadow_casting.py) depend entirely on
where the sun is in the sky at a given moment: how high above the
horizon (altitude) and which compass direction it's in (azimuth). This
module answers that question for any NYC lat/long, date, and hour using
the standard solar-position equations (the same ones behind most solar
panel siting and daylighting tools) -- no external package needed
because the formulas are short, well-documented physics, not something
that benefits from a dependency.

TIMEZONE SIMPLIFICATION -- READ THIS BEFORE TRUSTING THE HOUR PARAMETER
--------------------------------------------------------------------
`hour` is treated as Eastern STANDARD Time (UTC-5) year-round. NYC
observes daylight saving for 8 of its 12 months, which would shift
"clock time" by an extra hour during DST. Handling that correctly means
carrying a DST calendar through every call, which is more machinery
than a first-pass shade index needs. The practical effect: during DST
months (roughly March-November), the sun's *actual* position at
"2:00pm on someone's watch" is what this module would compute for
1:00pm. If/when the shade index needs to line up with real clock time
for a DST month, add one hour to `hour` before calling.
"""

import math


def solar_position(latitude, longitude, date, hour):
    """
    Compute the sun's position in the sky.

    Args:
        latitude, longitude: degrees, WGS84 (longitude negative = west).
        date: a datetime.date (or datetime.datetime) -- only month/day
            matter, used to find the day-of-year for solar declination.
        hour: local Eastern Standard Time, 24-hour decimal (e.g. 14.5
            for 2:30pm). See the timezone note in the module docstring.

    Returns:
        (altitude, azimuth) in degrees.
        altitude: 0 = sun on the horizon, 90 = directly overhead,
            negative = sun below the horizon (nighttime).
        azimuth: compass bearing of the sun, 0 = north, 90 = east,
            180 = south, 270 = west (standard clockwise-from-north).
    """
    day_of_year = date.timetuple().tm_yday

    # Solar declination (Cooper's equation) -- how far the sun's direct
    # ray is north/south of the equator today, driven by Earth's axial tilt.
    declination = 23.45 * math.sin(math.radians(360 / 365 * (284 + day_of_year)))

    # Equation of time (minutes) -- corrects mean solar time for the
    # combined effect of Earth's elliptical orbit and axial tilt, which
    # makes true solar noon drift up to ~16 minutes off clock noon.
    b = math.radians(360 / 365 * (day_of_year - 81))
    equation_of_time = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)

    # Local Standard Time Meridian for Eastern Standard Time (UTC-5).
    local_standard_meridian = 15 * (-5)

    time_correction = 4 * (longitude - local_standard_meridian) + equation_of_time  # minutes
    local_solar_time = hour + time_correction / 60  # hours
    hour_angle = 15 * (local_solar_time - 12)  # degrees; 0 at solar noon

    lat_rad = math.radians(latitude)
    dec_rad = math.radians(declination)
    ha_rad = math.radians(hour_angle)

    altitude_rad = math.asin(
        math.sin(dec_rad) * math.sin(lat_rad)
        + math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad)
    )
    altitude = math.degrees(altitude_rad)

    # Azimuth via the standard identity. Clamp the ratio before acos --
    # floating-point error can push it a hair past +/-1 right at sunrise
    # or sunset, which would otherwise raise a domain error.
    cos_azimuth = (math.sin(dec_rad) - math.sin(altitude_rad) * math.sin(lat_rad)) / (
        math.cos(altitude_rad) * math.cos(lat_rad)
    )
    cos_azimuth = max(-1.0, min(1.0, cos_azimuth))
    azimuth = math.degrees(math.acos(cos_azimuth))
    # acos only gives us the morning-side (0-180) answer; mirror it for
    # afternoon hour angles so azimuth correctly sweeps past 180.
    if hour_angle > 0:
        azimuth = 360 - azimuth

    return altitude, azimuth
