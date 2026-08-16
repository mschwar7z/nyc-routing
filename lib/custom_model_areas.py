"""
Turns per-block bike_cost/shade_score into GraphHopper `custom_model`
`areas` -- GeoJSON polygons referenced by an `in_<name>` condition in a
priority rule, evaluated per-request, no server rebuild or custom Java.

WHY AREAS, NOT A COMPILED ENCODED VALUE
------------------------------------------
The obvious-looking design -- tag each OSM way with bike_cost/
shade_score as a real GraphHopper encoded value at import time, then
reference it directly in custom_model (`"if": "bike_cost < 1"`) --
turns out not to be available without modifying GraphHopper's own
source: a custom encoded value needs a compiled TagParser +
EncodedValueFactory registered in DefaultImportRegistry.java, which
lives inside graphhopper-core, not something pluggable via config or a
mounted jar (confirmed against GraphHopper's docs/forum, not assumed --
see scripts/04's docstring). It would also bake shade_score's month/
hour buckets in at graph-build time, which reintroduces a rebuild per
shade preference -- the exact per-preference-tileset problem this
project picked GraphHopper specifically to avoid (see README).

`areas` sidesteps both problems: they're GeoJSON passed in the request
body itself, matched against edges by GraphHopper's own geometry code,
no encoded value or import step involved. The tradeoff is they don't
scale to one polygon per block -- forum reports flag GraphHopper's
~100k-character custom_model payload ceiling and describe hundreds of
individual regions as "not really the use case". So blocks are grouped
into a small number of cost BUCKETS first, and each bucket's block
lines are merged into one buffered multipolygon -- a handful of areas
per request rather than 889.

BUFFER WIDTH: 25ft half-width (50ft total strip). scripts/04 confirmed
889/889 blocks sit within 30ft of a real OSM way (max 10.6ft), so 25ft
comfortably covers normal digitization slop and dual-carriageway
avenues (split into separate OSM ways a lane-width apart) without
being wide enough to bleed onto a parallel street a full block over.

COVERAGE IS PARTIAL, ON PURPOSE: only blocks in bike_scored_blocks.geojson
/ shade_index.parquet (Upper West Side, ~889 blocks) get areas. Edges
elsewhere in the Manhattan graph match no area and fall back to
GraphHopper's unmodified default priority (multiply_by effectively
1.0) -- there is no bike/shade data for them, so leaving them alone is
correct, not a bug to paper over.

MULTIPLIER SCALE: both preferences use the same 0.5 (cheap/preferred)
-- 1.5 (expensive) scale bike_cost already uses (see
scripts/01_bike_preference_score.py), so bike and shade routes are
comparably aggressive about steering around expensive edges.
"""

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

WGS84 = "EPSG:4326"
STATE_PLANE_FT = "EPSG:2263"
AREA_BUFFER_FT = 25.0

# bike_cost only ever takes these four values (see FACILITY_CLASS_MAP /
# NO_MATCH in scripts/01_bike_preference_score.py) -- no need to
# quantile-bucket, the buckets already exist in the data.
BIKE_COST_BUCKETS = [0.5, 0.75, 0.9, 1.5]

SHADE_QUANTILE_BUCKETS = 5
SHADE_MIN_MULTIPLIER = 0.5
SHADE_MAX_MULTIPLIER = 1.5


def _buffered_union_geojson(lines_wgs84):
    """
    WGS84 LineStrings -> one buffered-union polygon, back in WGS84.

    Round caps/joins (shapely's default), not flat -- unlike
    lib/shadow_casting.py's score_segment(), which deliberately uses
    flat caps to measure exact street width without overhang, these
    polygons are only ever used as GraphHopper `in_<area>` regions and
    as the debug/visualization overlay in web/index_route.html, where
    a smooth rounded edge reads better and the extra sliver of
    coverage a round cap adds at each block's endpoint (bounded by
    AREA_BUFFER_FT) is immaterial to which edges match.
    """
    lines_ft = gpd.GeoSeries(lines_wgs84, crs=WGS84).to_crs(STATE_PLANE_FT)
    strip = unary_union([line.buffer(AREA_BUFFER_FT, cap_style="round") for line in lines_ft])
    strip_wgs84 = gpd.GeoSeries([strip], crs=STATE_PLANE_FT).to_crs(WGS84).iloc[0]
    return strip_wgs84.__geo_interface__


def build_bike_cost_areas(blocks_gdf):
    """
    blocks_gdf: bike_scored_blocks.geojson, loaded (WGS84, has
    bike_cost + geometry columns).

    Returns {area_name: (geojson_geometry, bike_cost)} -- one entry per
    non-empty bucket.
    """
    areas = {}
    for cost in BIKE_COST_BUCKETS:
        bucket = blocks_gdf[blocks_gdf["bike_cost"] == cost]
        if bucket.empty:
            continue
        name = f"bike_cost_{str(cost).replace('.', '_')}"
        areas[name] = (_buffered_union_geojson(bucket.geometry), cost)
    return areas


def build_shade_score_areas(blocks_gdf, shade_df, month, hour):
    """
    blocks_gdf: bike_scored_blocks.geojson, loaded (WGS84 geometry,
    joined by block_id -- this module doesn't care about bike_cost
    here, just reuses the same geometry per block_id).
    shade_df: shade_index.parquet, loaded (block_id, month, hour,
    shade_score).
    month, hour: which precomputed snapshot to bucket (must be one of
    the (month, hour) combinations scripts/02 actually computed).

    Returns {area_name: (geojson_geometry, mean_shade_score)} -- one
    entry per non-empty quantile bucket, plus the bucket's mean
    shade_score so callers can derive whichever direction (shade-
    seeking vs shade-avoiding) they want.
    """
    snapshot = shade_df[(shade_df["month"] == month) & (shade_df["hour"] == hour)]
    if snapshot.empty:
        raise ValueError(f"No shade_index rows for month={month}, hour={hour}")

    joined = blocks_gdf.merge(snapshot[["block_id", "shade_score"]], on="block_id", how="inner")
    joined["bucket"] = pd.qcut(joined["shade_score"], SHADE_QUANTILE_BUCKETS, labels=False, duplicates="drop")

    areas = {}
    for bucket_id, bucket_rows in joined.groupby("bucket"):
        if bucket_rows.empty:
            continue
        name = f"shade_bucket_{int(bucket_id)}"
        mean_score = bucket_rows["shade_score"].mean()
        areas[name] = (_buffered_union_geojson(bucket_rows.geometry), mean_score)
    return areas


def _areas_feature_collection(areas):
    """
    {name: (geometry, value)} -> a GeoJSON FeatureCollection with each
    Feature's `id` set to `name` -- GraphHopper's custom_model matches
    an `in_<id>` condition against that `id`, not a dict key (confirmed
    against GraphHopper's docs/core/custom-models.md, not assumed).

    `value` (bike_cost or mean shade_score, whichever built the bucket)
    is carried as a `properties.value` GraphHopper itself never reads --
    it's there so callers like web/index_route.html can color/label
    areas without recomputing the bucket logic.
    """
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "id": name, "properties": {"value": value}, "geometry": geom}
            for name, (geom, value) in areas.items()
        ],
    }


def custom_model_for_bike(blocks_gdf, base_priority=None):
    """
    A custom_model that prefers protected/painted bike infrastructure.
    Reuses bike_cost directly as multiply_by (it's already a 0.5-1.5
    cheap/expensive scale).
    """
    areas = build_bike_cost_areas(blocks_gdf)
    return {
        "areas": _areas_feature_collection(areas),
        "priority": (base_priority or []) + [
            {"if": f"in_{name}", "multiply_by": str(cost)}
            for name, (_geom, cost) in areas.items()
        ],
    }


def custom_model_for_shade(blocks_gdf, shade_df, month, hour, seek_shade=True, base_priority=None):
    """
    A custom_model that steers toward (seek_shade=True) or away from
    (seek_shade=False) shaded blocks at a given month/hour snapshot.

    Direction convention matches lib/shadow_casting.py's module
    docstring: shade-seeking cost = 1 - shade_score, shade-avoiding
    cost = shade_score directly. That [0,1] cost is then linearly
    rescaled onto the same [0.5, 1.5] multiplier scale bike_cost uses,
    so "cheapest possible" and "most expensive possible" mean the same
    magnitude of steering in both preferences.
    """
    areas = build_shade_score_areas(blocks_gdf, shade_df, month, hour)
    priority_rules = list(base_priority or [])
    for name, (_geom, mean_shade_score) in areas.items():
        cost = (1 - mean_shade_score) if seek_shade else mean_shade_score
        multiplier = SHADE_MIN_MULTIPLIER + cost * (SHADE_MAX_MULTIPLIER - SHADE_MIN_MULTIPLIER)
        priority_rules.append({"if": f"in_{name}", "multiply_by": f"{multiplier:.4f}"})
    return {
        "areas": _areas_feature_collection(areas),
        "priority": priority_rules,
    }


def custom_model_default():
    """No areas, no priority overrides -- GraphHopper's unmodified base weighting."""
    return {"areas": {}, "priority": []}
