"""
Step 3 (upgrade): make the map street-specific.

Run with the sample data:
  python3 scripts/03_aggregate_by_block.py

Run with the real datasets:
  python3 scripts/03_aggregate_by_block.py data/raw/full_trees.csv data/raw/full_blockface.geojson

WHY THIS IS BETTER THAN THE GRID
----------------------------------
The grid version (02_grid_aggregate.py) chops the city into arbitrary
squares that don't correspond to anything real -- a square might cut a
block in half, or merge two unrelated blocks together.

The tree census data is actually better than that already: every tree
record includes a `block_id`, which ties it to a specific real street
block. NYC Parks publishes a companion "Blockface Data" file that has
the *actual geometry* (a line, following the real street) for every
one of those blocks, plus which borough/street it's on.

So instead of inventing our own spatial units (squares), we use the
city's own units (blocks) and its own shapes (real street lines).
No point-in-polygon math, no shapefiles, no geopandas -- just a
group-by on a column that's already in the data, then a lookup.

TWO INPUT FILES
----------------
1. Tree data with block_id  (sample_trees_with_blocks.csv by default;
   pass a different path as the first argument)
2. Blockface geometry        (sample_blockface.geojson by default;
   pass a different path as the second argument)
"""

import json
import sys
import pandas as pd

trees_path = sys.argv[1] if len(sys.argv) > 1 else "data/raw/sample_trees_with_blocks.csv"
blockface_path = sys.argv[2] if len(sys.argv) > 2 else "data/raw/sample_blockface.geojson"
output_path = sys.argv[3] if len(sys.argv) > 3 else "data/processed/tree_blocks.geojson"

trees = pd.read_csv(trees_path)
print(f"Loaded {len(trees)} trees from {trees_path}")

with open(blockface_path) as f:
    blockface = json.load(f)
print(f"Loaded {len(blockface['features'])} block geometries from {blockface_path}")

# The real blockface export sometimes uses `blockfaceid` instead of
# `block_id` -- check both so this script works with either export.
sample_props = blockface["features"][0]["properties"]
block_id_key = "block_id" if "block_id" in sample_props else "blockfaceid"
if block_id_key not in sample_props:
    raise KeyError(
        f"Couldn't find a block ID field in the blockface data. "
        f"Available properties: {list(sample_props.keys())}. "
        f"Update block_id_key in this script to match."
    )
print(f"Using '{block_id_key}' as the join key")

# --- Step A: count trees per block_id ---------------------------------------
# Same idea as the grid version's groupby, but grouping on a real ID
# instead of an invented cell coordinate.
counts = trees.groupby("block_id").size().to_dict()
print(f"Trees found on {len(counts)} distinct blocks")

# --- Step B: score blocks relative to each other -----------------------------
# Blocks with zero trees in our tree data (like block 999999 in the sample)
# won't appear in `counts` at all -- we treat those as 0, which is
# correct: no trees recorded means no trees.
present_counts = list(counts.values())
# Include 0 in the range explicitly -- a block with no trees at all
# (like our sample's "NO TREE ST") should score 0, not go negative.
min_count = 0
max_count = max(present_counts) if present_counts else 1


def score(n):
    if max_count == min_count:
        return 50.0  # avoid divide-by-zero if every block has the same count
    return round(max(0, min(100, (n - min_count) / (max_count - min_count) * 100)), 1)


# --- Step C: attach tree_count + score onto each block's real geometry ------
features_out = []
for feature in blockface["features"]:
    block_id = feature["properties"][block_id_key]
    tree_count = counts.get(block_id, 0)  # 0 if this block had no trees at all
    features_out.append({
        "type": "Feature",
        "geometry": feature["geometry"],  # the REAL street line, untouched
        "properties": {
            "block_id": block_id,
            "st_name": feature["properties"].get("st_name", "Unknown"),
            "borough": feature["properties"].get("borough", "Unknown"),
            "tree_count": tree_count,
            "coverage_score": score(tree_count),
        },
    })

geojson_out = {"type": "FeatureCollection", "features": features_out}

with open(output_path, "w") as f:
    json.dump(geojson_out, f)

print(f"Wrote {len(features_out)} real street blocks to {output_path}")
for feat in sorted(features_out, key=lambda f: f["properties"]["coverage_score"]):
    p = feat["properties"]
    print(f"  {p['st_name']:15s} ({p['borough']:10s}) -- {p['tree_count']} trees -- score {p['coverage_score']}")
