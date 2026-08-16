"""
Turns a sun angle + building footprints into a shade score per street
segment. Geometry only -- the sun angle itself comes from
lib/solar_geometry.solar_position(), computed once per (month, hour)
and passed in here, since NYC is small enough that the sun's position
is effectively uniform citywide at a given moment (no need to
recompute it per building).

COORDINATE SYSTEM: every function here expects EPSG:2263 (NY State
Plane, US feet) inputs -- building polygons, street lines, and the
`building_height_ft` values are all assumed to already be in feet on a
projected plane. Shadow length math (feet, straight-line trig) is only
correct in a projected CRS; doing it in WGS84 degrees would silently
produce nonsense.

DIRECTION CONVENTION (decided, don't relitigate downstream):
`shade_score` from score_segment() means "fraction of this segment
that's in shadow" -- HIGH shade_score = heavily shaded. So:
  - a route that wants shade uses cost = 1 - shade_score
  - a route that wants to avoid shade uses cost = shade_score directly
"""

import math

from shapely.affinity import translate
from shapely.ops import unary_union
from shapely.strtree import STRtree

# Below this altitude, tan(altitude) is small enough that shadow length
# blows up to thousands of feet -- longer than most city blocks, which
# is both computationally wasteful (huge number of sweep steps) and
# not physically meaningful (at that point the whole street canyon is
# uniformly in shade regardless of which specific building casts it).
# We treat the sun as "too low to model" below this threshold, the same
# as the sun being below the horizon entirely.
MIN_SHADOW_ALTITUDE_DEG = 5.0


def shadow_vector(building_height_ft, altitude, azimuth):
    """
    Length and compass direction of the shadow a building casts.

    Args:
        building_height_ft: building height above ground, feet.
        altitude, azimuth: sun position in degrees, from solar_position().

    Returns:
        (length_ft, direction_deg), or None if the sun is below the
        horizon or too low to model meaningfully (see
        MIN_SHADOW_ALTITUDE_DEG) -- callers should skip shadow/shade
        computation entirely for that moment rather than treat it as
        zero shadow.
        direction_deg is the compass bearing the shadow points
        *toward* (opposite the sun), 0 = north, 90 = east, matching the
        azimuth convention in solar_geometry.
    """
    if altitude < MIN_SHADOW_ALTITUDE_DEG:
        return None

    length_ft = building_height_ft / math.tan(math.radians(altitude))
    direction_deg = (azimuth + 180) % 360
    return length_ft, direction_deg


def cast_shadow(building_polygon, shadow_vector, step_ft=5.0, max_steps=200):
    """
    The polygon a building's shadow covers on the ground.

    Non-convex/L-shaped footprints don't sweep into a simple polygon
    you can compute in closed form, so we approximate the true swept
    region: translate the footprint in `step_ft` increments from 0 up
    to the full shadow length, and union all the copies together. This
    is exact in the limit of step_ft -> 0; 5ft steps are fine grained
    enough to not leave gaps at city-block scale while staying cheap.

    Args:
        building_polygon: footprint, EPSG:2263 feet.
        shadow_vector: (length_ft, direction_deg) from shadow_vector().
        step_ft: sweep step size, feet.
        max_steps: hard cap on sweep copies, so a single very tall
            building at a low (but still >= MIN_SHADOW_ALTITUDE_DEG)
            sun angle can't blow up the union into thousands of copies.

    Returns:
        shadow_polygon (or multipolygon) in EPSG:2263 feet.
    """
    length_ft, direction_deg = shadow_vector
    if length_ft <= 0:
        return building_polygon

    direction_rad = math.radians(direction_deg)
    # EPSG:2263 axes are easting (x) / northing (y), so a compass
    # bearing converts the usual way: x-component is sin, y is cos.
    dx_unit = math.sin(direction_rad)
    dy_unit = math.cos(direction_rad)

    num_steps = min(max_steps, max(2, math.ceil(length_ft / step_ft) + 1))
    copies = []
    for i in range(num_steps):
        t = length_ft * i / (num_steps - 1)
        copies.append(translate(building_polygon, xoff=t * dx_unit, yoff=t * dy_unit))

    return unary_union(copies)


class ShadowIndex:
    """
    An STRtree over a set of shadow polygons, bundled with the polygon
    list -- shapely's STRtree.query() returns positional indices into
    whatever sequence it was built from, not geometries or references
    back to them, so the tree is useless on its own without also
    keeping that original list around.

    Built once per (month, hour) in the orchestrator (all shadow
    polygons for that moment are the same regardless of which street
    segment is being scored), then reused across every score_segment()
    call for that moment instead of rebuilding an index per segment.
    """

    def __init__(self, shadow_polygons):
        self.polygons = list(shadow_polygons)
        self.tree = STRtree(self.polygons)

    def query(self, geom):
        return [self.polygons[i] for i in self.tree.query(geom)]


def build_shadow_index(shadow_polygons):
    return ShadowIndex(shadow_polygons)


def score_segment(street_line, shadow_index, buffer_ft=15):
    """
    Fraction of `street_line` that falls inside any shadow.

    We buffer the line into a thin flat-capped strip rather than
    intersecting the raw line against shadow polygons directly: a
    zero-width line intersected with a polygon is prone to floating
    point edge cases (a line running exactly along a shadow boundary),
    and the buffered-area approach also gives a physically meaningful
    answer -- "how much of the walkable street width is shaded" -- for
    the width a pedestrian/cyclist actually occupies.

    Deliberately takes no street-orientation/bearing parameter: the
    line's own coordinates already encode its direction, and the
    buffer + intersection is symmetric under rotation, so two streets
    at different angles get correctly different shade fractions from
    geometry alone.

    Args:
        street_line: LineString, EPSG:2263 feet.
        shadow_index: a ShadowIndex (see build_shadow_index()).
        buffer_ft: half-width of the strip, feet.

    Returns:
        shade_fraction in [0.0, 1.0]. HIGH = heavily shaded (see the
        module docstring for the cost-function convention).
    """
    strip = street_line.buffer(buffer_ft, cap_style="flat")
    if strip.area == 0:
        return 0.0

    nearby = shadow_index.query(strip)
    if not nearby:
        return 0.0

    shadow_union = unary_union(nearby)
    overlap = strip.intersection(shadow_union)
    return overlap.area / strip.area
