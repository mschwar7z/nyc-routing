"""
Step 4 (routing engine): sanity-check that our block geometry actually
sits on real Manhattan streets before we build anything on top of it.

WHY THIS CHECK EXISTS, AND WHY IT'S NOT AN OSM-WAY JOIN
---------------------------------------------------------
The original plan was to snap each scored block to a specific nearest
OSM way (by way_id), the way scripts/01 snaps blocks to bike routes,
so bike_cost/shade_score could be tagged onto that exact way before
GraphHopper import. That plan assumed GraphHopper could take custom
per-edge encoded values from arbitrary external data via config alone.
Checking GraphHopper's current docs/source (not assuming) turned up
that it can't -- registering a real custom encoded value requires a
compiled TagParser + EncodedValueFactory wired into
DefaultImportRegistry.java, which lives in graphhopper-core itself, so
doing it without forking/rebuilding GraphHopper isn't possible. That
also would have baked shade_score's month/hour buckets in at graph-
build time, reintroducing per-preference rebuilds -- the exact problem
this project picked GraphHopper's request-time custom_model to avoid.

So the actual join happens differently (see lib/custom_model_areas.py
and scripts/05): blocks are bucketed by cost, each bucket's block
lines are buffered into strip polygons and passed as `areas` in the
custom_model JSON at request time. GraphHopper matches areas to edges
by geometry itself -- no OSM way_id is ever needed.

That still leaves one real question: is our block geometry (from 2015
Street Tree Census blockface data) actually close enough to the real
street centerlines in the Manhattan OSM extract for a geometric buffer
to land on the right edges? This script checks that, the same way
scripts/01 checks blockface-to-bike-route distance -- nearest-neighbor
in EPSG:2263 feet, ~30ft tolerance per the original brief.

Run:
  python3 scripts/04_validate_block_osm_alignment.py
"""

import json
import sys

import geopandas as gpd
from shapely.strtree import STRtree

blocks_path = sys.argv[1] if len(sys.argv) > 1 else "data/processed/bike_scored_blocks.geojson"
osm_ways_path = sys.argv[2] if len(sys.argv) > 2 else "data/processed/manhattan_osm_ways.geojson"

WGS84 = "EPSG:4326"
STATE_PLANE_FT = "EPSG:2263"
TOLERANCE_FT = 30.0

blocks = gpd.read_file(blocks_path)
osm_ways = gpd.read_file(osm_ways_path)
print(f"Loaded {len(blocks)} scored blocks and {len(osm_ways)} OSM ways")

blocks_ft = blocks.to_crs(STATE_PLANE_FT)
osm_ways_ft = osm_ways.to_crs(STATE_PLANE_FT)

way_tree = STRtree(osm_ways_ft.geometry.tolist())

distances = []
for line in blocks_ft.geometry:
    nearest_idx = way_tree.nearest(line)
    nearest_geom = osm_ways_ft.geometry.iloc[nearest_idx]
    distances.append(line.distance(nearest_geom))

within_tolerance = sum(1 for d in distances if d <= TOLERANCE_FT)
print(f"\n{within_tolerance}/{len(blocks)} blocks ({within_tolerance / len(blocks) * 100:.1f}%) "
      f"have a real OSM street within {TOLERANCE_FT:.0f}ft")

sorted_d = sorted(distances)
n = len(sorted_d)
print(f"Distance to nearest OSM way (ft): "
      f"min={sorted_d[0]:.1f}  median={sorted_d[n // 2]:.1f}  "
      f"p90={sorted_d[int(n * 0.9)]:.1f}  max={sorted_d[-1]:.1f}")

far_blocks = [
    (row["block_id"], d) for row, d in zip(blocks.to_dict("records"), distances)
    if d > TOLERANCE_FT
]
if far_blocks:
    print(f"\n{len(far_blocks)} blocks beyond {TOLERANCE_FT:.0f}ft (first 10):")
    for block_id, d in far_blocks[:10]:
        print(f"  block_id={block_id}  {d:.1f}ft")
