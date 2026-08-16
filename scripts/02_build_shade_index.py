"""
Step 2: orchestrator. No shadow/solar math lives here -- that's all in
lib/solar_geometry.py and lib/shadow_casting.py, which are meant to be
reusable and independently testable (see tests/test_shadow_casting.py).
This script just drives them across a grid of (month, hour) moments and
writes the results out.

Run:
  python3 scripts/02_build_shade_index.py

Inputs (Upper West Side test area, see README.md for how they were
downloaded):
  data/raw/buildings_uws.geojson   Building footprints + height_roof
                                    (Socrata 5zhs-2jue).
  data/raw/blockface_uws.json      Same blockface data as script 01
                                    (Socrata ju3b-rwpy).

Outputs:
  data/processed/shade_index.parquet
      block_id, month, hour, shade_score -- the actual point of this
      pipeline. Routing should do a fast lookup here at request time
      instead of recomputing shadow geometry live.
  data/processed/shade_scored_blocks_jun_2pm.geojson
      One representative (month, hour) slice, purely for eyeballing on
      a map that the numbers look sane (see web/index_shade.html).

FIRST-PASS SCOPE
-------------------
Only 3 months (Jan/Jun/Oct 15th, as winter/summer/shoulder stand-ins)
and hourly 7am-7pm. See MONTHS below -- expanding to all 12 months
later is a one-line change.
"""

import datetime
import json
import sys

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString

sys.path.insert(0, "lib")
from shadow_casting import build_shadow_index, cast_shadow, score_segment, shadow_vector  # noqa: E402
from solar_geometry import solar_position  # noqa: E402

buildings_path = sys.argv[1] if len(sys.argv) > 1 else "data/raw/buildings_uws.geojson"
blockface_path = sys.argv[2] if len(sys.argv) > 2 else "data/raw/blockface_uws.json"
index_output_path = sys.argv[3] if len(sys.argv) > 3 else "data/processed/shade_index.parquet"
representative_output_path = (
    sys.argv[4] if len(sys.argv) > 4 else "data/processed/shade_scored_blocks_jun_2pm.geojson"
)

WGS84 = "EPSG:4326"
STATE_PLANE_FT = "EPSG:2263"

# Winter/summer/shoulder stand-ins for the first pass. To expand to all
# twelve months later, this is the only line that needs to change:
#   MONTHS = [(m, 15) for m in range(1, 13)]
MONTHS = [(1, 15), (6, 15), (10, 15)]
HOURS = range(7, 20)  # 7am-7pm, hourly, Eastern Standard Time (see lib/solar_geometry.py)

# Only day-of-year feeds the solar math (see lib/solar_geometry.py), so
# the specific year is arbitrary -- it shifts Jan 15/Oct 15's exact
# day-of-year by at most 1 across leap years, which has no visible
# effect on declination.
REFERENCE_YEAR = 2024

# Sun position is computed once per (month, hour) rather than per
# building, since NYC is small enough that it's uniform citywide at a
# given moment. This is the point we compute it at -- roughly the
# Upper West Side test area's centroid.
CITY_REFERENCE_LAT, CITY_REFERENCE_LON = 40.7850, -73.9775

REPRESENTATIVE_MONTH, REPRESENTATIVE_HOUR = 6, 14  # -> shade_scored_blocks_jun_2pm.geojson


# --- load buildings ----------------------------------------------------------
buildings = gpd.read_file(buildings_path)
buildings["height_roof"] = buildings["height_roof"].astype(float)
buildings_ft = buildings.to_crs(STATE_PLANE_FT)
print(f"Loaded {len(buildings_ft)} buildings from {buildings_path}")

# --- load blockface (same 3-point-line construction as script 01) -----------
with open(blockface_path) as f:
    blockface_rows = json.load(f)
blockface_df = pd.DataFrame(blockface_rows)
for col in ["start_lat", "start_long", "mid_lat", "mid_long", "end_lat", "end_long"]:
    blockface_df[col] = blockface_df[col].astype(float)
blockface_wgs84 = gpd.GeoDataFrame(
    blockface_df,
    geometry=[
        LineString([(r["start_long"], r["start_lat"]), (r["mid_long"], r["mid_lat"]), (r["end_long"], r["end_lat"])])
        for _, r in blockface_df.iterrows()
    ],
    crs=WGS84,
)
blockface_ft = blockface_wgs84.to_crs(STATE_PLANE_FT)
print(f"Loaded {len(blockface_ft)} blockface segments from {blockface_path}")

# --- the (month, hour) loop --------------------------------------------------
rows = []
representative_features = []

for month, day in MONTHS:
    date = datetime.date(REFERENCE_YEAR, month, day)
    for hour in HOURS:
        altitude, azimuth = solar_position(CITY_REFERENCE_LAT, CITY_REFERENCE_LON, date, hour)

        shadow_polygons = []
        for geom, height_ft in zip(buildings_ft.geometry, buildings_ft["height_roof"]):
            sv = shadow_vector(height_ft, altitude, azimuth)
            if sv is None:
                continue
            shadow_polygons.append(cast_shadow(geom, sv))

        if not shadow_polygons:
            print(f"  {date} {hour:02d}:00 -- sun altitude {altitude:5.1f} deg, no usable shadows, skipping")
            continue

        shadow_index = build_shadow_index(shadow_polygons)
        is_representative = (month == REPRESENTATIVE_MONTH and hour == REPRESENTATIVE_HOUR)

        for i, block_line_ft in enumerate(blockface_ft.geometry):
            block = blockface_ft.iloc[i]
            shade_score = score_segment(block_line_ft, shadow_index)
            rows.append({
                "block_id": block["block_id"],
                "month": month,
                "hour": hour,
                "shade_score": shade_score,
            })
            if is_representative:
                representative_features.append({
                    "type": "Feature",
                    "geometry": blockface_wgs84.iloc[i].geometry.__geo_interface__,
                    "properties": {
                        "block_id": block["block_id"],
                        "borough": block.get("boroname", "Unknown"),
                        "shade_score": round(shade_score, 3),
                    },
                })

        print(f"  {date} {hour:02d}:00 -- sun altitude {altitude:5.1f} deg, azimuth {azimuth:5.1f} deg, "
              f"scored {len(blockface_ft)} blocks against {len(shadow_polygons)} shadows")

# --- write outputs ------------------------------------------------------------
index_df = pd.DataFrame(rows)
index_df.to_parquet(index_output_path, index=False)
print(f"\nWrote {len(index_df)} (block, month, hour) rows to {index_output_path}")

representative_geojson = {"type": "FeatureCollection", "features": representative_features}
with open(representative_output_path, "w") as f:
    json.dump(representative_geojson, f)
print(f"Wrote {len(representative_features)} blocks for the "
      f"{REPRESENTATIVE_MONTH}/{REPRESENTATIVE_HOUR}:00 sanity map to {representative_output_path}")
