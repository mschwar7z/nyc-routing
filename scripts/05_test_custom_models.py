"""
Step 6 (routing engine): build the three custom_model payloads (bike,
shade, default) and POST each straight to the running GraphHopper API,
before any backend code exists -- confirms each one produces a sane,
different route on its own, per the task brief's instruction not to
build on top of an unverified payload.

Requires: `docker compose up` running in graphhopper/ (see
graphhopper/docker-compose.yml), listening on localhost:8989.

Run:
  python3 scripts/05_test_custom_models.py
"""

import json
import sys
import urllib.request

import geopandas as gpd
import pandas as pd

sys.path.insert(0, ".")
from lib.custom_model_areas import (
    custom_model_default,
    custom_model_for_bike,
    custom_model_for_shade,
)

GRAPHHOPPER_URL = "http://localhost:8989/route"

# Deliberately midway between Amsterdam Ave (protected bike lane,
# bike_cost=0.5 almost throughout) and Columbus Ave (no bike
# infrastructure at all, bike_cost=1.5 throughout) -- two parallel
# avenues a block apart -- so a ~12-block north-south leg genuinely
# forces a choice between them, rather than testing on a pair of
# points with only one reasonable street to take regardless of
# preference.
ORIGIN = (40.783, -73.9771)  # between Amsterdam/Columbus near W79th
DEST = (40.795, -73.9688)  # between Amsterdam/Columbus near W91st

SHADE_MONTH, SHADE_HOUR = 6, 14  # matches the project's existing "jun 2pm" snapshot convention


def call_graphhopper(custom_model, label):
    body = {
        "points": [[ORIGIN[1], ORIGIN[0]], [DEST[1], DEST[0]]],
        "profile": "bike",
        "points_encoded": False,
        "custom_model": custom_model,
    }
    req = urllib.request.Request(
        GRAPHHOPPER_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        print(f"[{label}] HTTP {e.code}: {e.read().decode()}")
        return None

    path = result["paths"][0]
    print(f"[{label}] distance={path['distance']:.0f}m  weight={path['weight']:.2f}  "
          f"time={path['time'] / 1000:.1f}s  points={len(path['points']['coordinates'])}")
    return path


blocks = gpd.read_file("data/processed/bike_scored_blocks.geojson")
shade_df = pd.read_parquet("data/processed/shade_index.parquet")

print("Testing three custom_model payloads against the running GraphHopper API\n")

call_graphhopper(custom_model_default(), "default")
bike_model = custom_model_for_bike(blocks)
print(f"  (bike model has {len(bike_model['areas']['features'])} areas)")
call_graphhopper(bike_model, "bike")
shade_model = custom_model_for_shade(blocks, shade_df, SHADE_MONTH, SHADE_HOUR, seek_shade=True)
print(f"  (shade model has {len(shade_model['areas']['features'])} areas)")
call_graphhopper(shade_model, "shade (seeking, jun 2pm)")
