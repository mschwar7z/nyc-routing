"""
Sanity check for lib/shadow_casting.py, requested before trusting
score_segment(): it must NOT need a street-orientation parameter to
produce orientation-sensitive results. The line's own coordinates
already encode direction, so two streets at right angles through the
same shadow should come out with different (and non-degenerate)
shade_fraction values purely from geometry -- if that weren't true,
something would be wrong with the buffer/intersection logic, not
missing input.

Run directly: python3 tests/test_shadow_casting.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from shapely.geometry import LineString, box

from shadow_casting import build_shadow_index, cast_shadow, score_segment, shadow_vector


def test_orientation_sensitivity_without_bearing_input():
    # One building, one sun angle -- everything below is geometry only.
    building = box(-20, -20, 20, 20)  # 40x40 ft footprint, EPSG:2263 feet
    altitude, azimuth = 30.0, 135.0  # sun in the southeast
    height_ft = 100.0

    sv = shadow_vector(height_ft, altitude, azimuth)
    assert sv is not None, "sun angle should be above MIN_SHADOW_ALTITUDE_DEG"
    shadow_poly = cast_shadow(building, sv)
    index = build_shadow_index([shadow_poly])

    # Two streets at right angles to each other, both crossing near the
    # shadow. Neither call below passes a bearing/orientation -- the
    # LineString coordinates are the only source of direction.
    street_ew = LineString([(-150, 100), (150, 100)])  # east-west
    street_ns = LineString([(-60, -150), (-60, 150)])  # north-south, perpendicular

    frac_ew = score_segment(street_ew, index)
    frac_ns = score_segment(street_ns, index)

    # Both streets clip the shadow (non-degenerate: neither 0 nor 1),
    # and the two perpendicular streets get distinct fractions purely
    # from their geometry.
    assert 0.0 < frac_ew < 1.0, f"expected a partial shadow overlap, got {frac_ew}"
    assert 0.0 < frac_ns < 1.0, f"expected a partial shadow overlap, got {frac_ns}"
    assert frac_ew != frac_ns, (
        f"east-west and north-south streets got identical shade_fraction "
        f"({frac_ew}) -- score_segment should be orientation-sensitive "
        f"without being told the orientation"
    )

    print(f"east-west shade_fraction:   {frac_ew:.4f}")
    print(f"north-south shade_fraction: {frac_ns:.4f}")
    print("PASS: perpendicular streets get different, non-degenerate shade scores")


if __name__ == "__main__":
    test_orientation_sensitivity_without_bearing_input()
