"""
Step 1: score every street block by how good its bike infrastructure is.

Run:
  python3 scripts/01_bike_preference_score.py

Inputs (already downloaded into data/raw/ for the Upper West Side test
area -- see README.md for the exact Socrata calls):
  data/raw/blockface_uws.json      2015 Street Tree Census blockface
                                    data (Socrata ju3b-rwpy). Real block
                                    geometry, but NO street name field.
  data/raw/bike_routes_uws.geojson NYC Bike Routes (Socrata mzxg-pwib;
                                    the other candidate ID we were given,
                                    9e2b-mctv, returned empty records on
                                    probing and isn't the live dataset).

WHY A GEOMETRIC JOIN, NOT A NAME JOIN
----------------------------------------
The plan going in was to join blockface <-> bike routes by street name.
That assumed blockface would have a street-name field the way the old
tree-map project's SAMPLE data did (see the reference script's
`st_name` property). The real blockface export doesn't have a name
field at all -- probing turned that up before any join code got
written. So instead of name-matching, every blockface segment is
matched to its nearest bike route line by actual geometry (shapely
STRtree nearest-neighbor, in EPSG:2263 feet so "nearest" means real
feet, not distorted lat/long degrees). This is actually the more
correct approach the task brief already flagged as the production fix
for a future name-matching simplification -- doing it now just means
skipping the fragile version.

It's still an approximation, not true topology: we match each block to
whichever bike route line is closest within MAX_MATCH_DISTANCE_FT, not
a line that's been snapped/split to share endpoints with the block. On
a wide two-way avenue with a protected lane on only one side, the
block's centerline could end up matched to that one side's lane even
though the other side has none -- production would want lane-side-aware
snapping, not just nearest-line.

Because blockface has no street name of its own, `st_name` in the
output is borrowed from whichever bike route line each block matched
to (or left null if no bike route was close enough to match).
"""

import json
import sys

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString
from shapely.strtree import STRtree

blockface_path = sys.argv[1] if len(sys.argv) > 1 else "data/raw/blockface_uws.json"
bike_routes_path = sys.argv[2] if len(sys.argv) > 2 else "data/raw/bike_routes_uws.geojson"
output_path = sys.argv[3] if len(sys.argv) > 3 else "data/processed/bike_scored_blocks.geojson"

WGS84 = "EPSG:4326"
STATE_PLANE_FT = "EPSG:2263"

# A block only matches a bike route if the nearest one is within this
# many feet of the block's centerline. Wide avenues can be 80-100ft
# curb-to-curb, so a lane on the far side is still plausibly "this
# street's" infrastructure; beyond that we'd likely be grabbing an
# unrelated parallel street.
MAX_MATCH_DISTANCE_FT = 60.0

# The brief's cost scheme (Protected/Painted/Sharrow/no-match) predates
# probing the real data, which uses NYC DOT's actual facility classes
# instead of those three buckets. This maps the real classes onto the
# brief's buckets:
#   - "Protected" is an exact match.
#   - Conventional/Curbside (+ Buffered) and Wide Parking Lane are all
#     paint-on-asphalt lanes without physical separation -> "Painted".
#   - "Shared" (shared-lane markings) is the literal sharrow. "Signed
#     Route" (no lane markings, just wayfinding signage on a quiet
#     street) has no paint either, so it's grouped with sharrows as the
#     weakest form of official bike accommodation.
#   - "Link" is an unclassified connector segment between named routes;
#     treated as generic painted infra since its own class doesn't say
#     more than "some route continues here".
#   - "None" is NYC DOT explicitly recording no bike infrastructure on
#     that line -- same outcome as no nearby bike route at all.
FACILITY_CLASS_MAP = {
    "Protected": ("Protected", 0.5),
    "Conventional": ("Painted", 0.75),
    "Conventional Buffered": ("Painted", 0.75),
    "Curbside": ("Painted", 0.75),
    "Curbside Buffered": ("Painted", 0.75),
    "Wide Parking Lane": ("Painted", 0.75),
    "Shared": ("Sharrow", 0.9),
    "Signed Route": ("Sharrow", 0.9),
    "Link": ("Painted", 0.75),
    "None": (None, 1.5),
}
NO_MATCH = (None, 1.5)


def blockface_linestring(row):
    # Real blockface data has no single geometry field -- just three
    # lat/long points along the block (start, mid, end). A 3-point line
    # through them is a fine approximation of the actual street curve.
    return LineString([
        (row["start_long"], row["start_lat"]),
        (row["mid_long"], row["mid_lat"]),
        (row["end_long"], row["end_lat"]),
    ])


with open(blockface_path) as f:
    blockface_rows = json.load(f)
blockface_df = pd.DataFrame(blockface_rows)
for col in ["start_lat", "start_long", "mid_lat", "mid_long", "end_lat", "end_long"]:
    blockface_df[col] = blockface_df[col].astype(float)
blockface = gpd.GeoDataFrame(
    blockface_df,
    geometry=[blockface_linestring(r) for _, r in blockface_df.iterrows()],
    crs=WGS84,
)
print(f"Loaded {len(blockface)} blockface segments from {blockface_path}")

bike_routes = gpd.read_file(bike_routes_path)
print(f"Loaded {len(bike_routes)} bike route lines from {bike_routes_path}")

# Nearest-neighbor matching needs real distances, so reproject both
# into State Plane feet before measuring anything.
blockface_ft = blockface.to_crs(STATE_PLANE_FT)
bike_routes_ft = bike_routes.to_crs(STATE_PLANE_FT)

bike_tree = STRtree(bike_routes_ft.geometry.tolist())

matches = []
for line in blockface_ft.geometry:
    nearest_idx = bike_tree.nearest(line)
    nearest_geom = bike_routes_ft.geometry.iloc[nearest_idx]
    distance_ft = line.distance(nearest_geom)
    if distance_ft <= MAX_MATCH_DISTANCE_FT:
        matches.append(bike_routes_ft.iloc[nearest_idx])
    else:
        matches.append(None)

facility_names = []
bike_costs = []
st_names = []
matched_count = 0
for match in matches:
    if match is None:
        facility, cost = NO_MATCH
        st_name = None
    else:
        matched_count += 1
        raw_class = match.get("ft_facilit")
        facility, cost = FACILITY_CLASS_MAP.get(raw_class, NO_MATCH)
        st_name = match.get("street")
    facility_names.append(facility)
    bike_costs.append(cost)
    st_names.append(st_name)

print(f"Matched {matched_count}/{len(blockface)} blocks to a nearby bike route "
      f"(within {MAX_MATCH_DISTANCE_FT:.0f}ft)")

features_out = []
for i, row in blockface.reset_index(drop=True).iterrows():
    features_out.append({
        "type": "Feature",
        "geometry": row.geometry.__geo_interface__,  # original WGS84 line, for web display
        "properties": {
            "block_id": row["block_id"],
            "st_name": st_names[i],
            "borough": row.get("boroname", "Unknown"),
            "bike_facility": facility_names[i],
            "bike_cost": bike_costs[i],
        },
    })

geojson_out = {"type": "FeatureCollection", "features": features_out}
with open(output_path, "w") as f:
    json.dump(geojson_out, f)
print(f"Wrote {len(features_out)} scored blocks to {output_path}")

summary = pd.Series(facility_names).fillna("No match").value_counts()
print("\nFacility breakdown:")
for facility, count in summary.items():
    print(f"  {facility:12s} {count}")
