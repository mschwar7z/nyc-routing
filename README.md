# nyc-routing

A custom-weighted NYC routing system: routes that answer questions like
"least/most shade route," "prefer protected bike paths," "avoid
high-traffic times" -- not by picking from a fixed set of pre-built
routing profiles, but by letting the request itself carry a weighting
formula.

## Architecture

```
data sources -> weighting pipeline (this repo, so far) -> routing engine -> backend API -> frontend
```

The routing engine will be [GraphHopper](https://www.graphhopper.com/),
self-hosted, not built from scratch. GraphHopper supports a *custom
model* passed in at request time -- a weighting expression evaluated
per edge -- rather than requiring a separate pre-built profile per
preference. That's why every score this repo produces is kept as
**per-edge data to reference in a formula later**, not baked into a
single fixed cost: `bike_cost` and `shade_score` are meant to be joined
onto GraphHopper's OSM edges at request time and combined however a
given query wants (e.g. `0.7 * shade_cost + 0.3 * bike_cost`).

This repo currently builds two weighting modules and stops there --
no routing engine, backend, or frontend yet. That's a separate
follow-up.

## Directory structure

```
lib/                     Reusable, testable modules (no I/O)
scripts/                 Orchestrators -- read data/raw, write data/processed
data/raw/                Downloaded source data (see Datasets below)
data/processed/          Script outputs
web/                     Standalone Leaflet pages for visually sanity-checking output
tests/                   Sanity checks for lib/
```

Scripts are numbered by run order, starting at 01.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install pandas shapely geopandas pyproj pyarrow
```

## Datasets

All three come from NYC Open Data (Socrata). Everything downloaded
into `data/raw/` is real data, scoped to an Upper West Side bounding
box (`40.77, -73.99` to `40.80, -73.965`) rather than the full city --
see "Why the Upper West Side" below.

| Dataset | Resource ID | Used for |
|---|---|---|
| Building footprints | `5zhs-2jue` | Shade (building height + polygon) |
| 2015 Street Tree Census -- Blockface | `ju3b-rwpy` | Base street geometry, both modules |
| NYC Bike Routes | `mzxg-pwib` | Bike preference |

Two things the task brief assumed turned out not to match the real
data, found by probing before writing any join code against them:

- **Bike routes has two candidate resource IDs in circulation** --
  `mzxg-pwib` and `9e2b-mctv`. Only `mzxg-pwib` returns real records;
  `9e2b-mctv` returns empty objects for every row. Use `mzxg-pwib`.
- **Blockface has no street-name field.** The old tree-map project's
  *sample* blockface data had an `st_name` property, but the real
  Socrata export (`ju3b-rwpy`) doesn't -- it has `block_id`, tree
  counts, borough, and the block's start/mid/end points in both
  lat/long and State Plane feet, but nothing naming the street. This
  is why the bike join is geometric, not name-based -- see Task 1
  below.

### Re-downloading

```bash
NW_LAT=40.80; NW_LON=-73.99; SE_LAT=40.77; SE_LON=-73.965

curl -s "https://data.cityofnewyork.us/resource/5zhs-2jue.geojson?%24where=within_box(the_geom,${NW_LAT},${NW_LON},${SE_LAT},${SE_LON})&%24limit=20000" \
  -o data/raw/buildings_uws.geojson

curl -s "https://data.cityofnewyork.us/resource/mzxg-pwib.geojson?%24where=within_box(the_geom,${NW_LAT},${NW_LON},${SE_LAT},${SE_LON})&%24limit=20000" \
  -o data/raw/bike_routes_uws.geojson

curl -s "https://data.cityofnewyork.us/resource/ju3b-rwpy.json?%24where=mid_lat%20between%20${SE_LAT}%20and%20${NW_LAT}%20AND%20mid_long%20between%20${NW_LON}%20and%20${SE_LON}&%24limit=20000" \
  -o data/raw/blockface_uws.json
```

(Blockface has no `the_geom` field to filter on with `within_box`, so
it's filtered on `mid_lat`/`mid_long` instead -- see the field list
above.)

### Why the Upper West Side

Buildings citywide is a large download and the shadow-casting math in
`lib/shadow_casting.py` is the expensive part of this pipeline (sweeping
and unioning translated copies of every building footprint, per sun
angle) -- a bounded test area keeps the first pass fast to iterate on
and easy to sanity-check on a map. Blockface and bike routes are scoped
to the same box so both modules cover the same streets and are visually
comparable side by side.

## Run order

```bash
source .venv/bin/activate
python3 scripts/01_bike_preference_score.py
python3 scripts/02_build_shade_index.py
python3 tests/test_shadow_casting.py
```

Then open `web/index_bike.html` and `web/index_shade.html` (via a local
server, e.g. `python3 -m http.server` from the repo root, so the
relative `../data/processed/...` fetches resolve) to see the results on
a map.

### Task 1 -- `scripts/01_bike_preference_score.py`

Scores every blockface segment by bike infrastructure quality. Since
blockface has no street name, each block is matched to its **nearest**
bike route line by real geometry (shapely STRtree nearest-neighbor, in
EPSG:2263 feet so distance is measured in real feet) within 60ft,
rather than by name. `st_name` in the output is borrowed from whichever
bike route the block matched to, and is null if nothing matched.

```
Protected -> 0.5   (cheap/preferred)
Painted   -> 0.75
Sharrow   -> 0.9
no match  -> 1.5   (expensive but not blocked)
```

The real NYC DOT facility classes (`Protected`, `Conventional`,
`Conventional Buffered`, `Curbside`, `Curbside Buffered`, `Shared`,
`Signed Route`, `Link`, `Wide Parking Lane`, `None`) don't literally
match that four-bucket scheme -- see the mapping and its rationale in
the script's `FACILITY_CLASS_MAP`.

**Simplification, flagged for production:** nearest-line matching can
pick the wrong side of a wide avenue that has a protected lane on only
one side. Production should snap by shared topology (splitting/matching
segments), not just "closest line within a threshold."

Output: `data/processed/bike_scored_blocks.geojson` --
`block_id, st_name, borough, bike_facility, bike_cost, geometry`.

### Task 2 -- `lib/solar_geometry.py`, `lib/shadow_casting.py`, `scripts/02_build_shade_index.py`

A library, not a one-off script, because shade needs computing across
many (month, hour) combinations and benefits from being testable in
isolation (`tests/test_shadow_casting.py`).

- **`lib/solar_geometry.py`** -- pure math: `solar_position(latitude,
  longitude, date, hour) -> (altitude, azimuth)`. Standard solar
  geometry (day-of-year -> declination, time + longitude -> hour
  angle), no external astronomy library. Treats `hour` as Eastern
  *Standard* Time year-round (a documented simplification -- see the
  module docstring for what that means during DST months).
- **`lib/shadow_casting.py`** -- geometry, given a sun angle:
  - `shadow_vector` -- shadow length + direction from building height
    and sun position.
  - `cast_shadow` -- handles non-convex/L-shaped footprints correctly
    by sweeping translated copies of the footprint along the shadow
    direction and unioning them, rather than a convex-hull shortcut.
  - `score_segment` -- buffers a street line into a thin strip and
    intersects it against nearby shadows (via `ShadowIndex`, an
    `STRtree` wrapper) to get the fraction of the segment in shadow.
  - **Direction convention:** `shade_score` is HIGH when a block is
    heavily shaded. A shade-seeking route costs edges as `1 -
    shade_score`; a shade-avoiding route costs them as `shade_score`
    directly.
  - `score_segment` deliberately takes no street-orientation parameter
    -- `tests/test_shadow_casting.py` verifies two perpendicular
    streets through the same shadow get different, non-degenerate
    scores from geometry alone.
  - All shadow geometry runs in EPSG:2263 feet; only the final GeoJSON
    output is reprojected back to WGS84.
- **`scripts/02_build_shade_index.py`** -- orchestrator only, no math.
  First pass covers 3 months (Jan/Jun/Oct 15th, as winter/summer/
  shoulder stand-ins -- `MONTHS` is a one-line change to expand to all
  12) x hourly 7am-7pm. Calls `solar_position` once per (month, hour)
  -- not per building, since sun angle is effectively uniform citywide
  at a given moment -- then `score_segment` for every block against
  nearby buildings.

Outputs:
- `data/processed/shade_index.parquet` -- `block_id, month, hour,
  shade_score`. This is the point of the architecture: routing should
  do a fast lookup here instead of recomputing shadows live.
- `data/processed/shade_scored_blocks_jun_2pm.geojson` -- one
  representative snapshot (June, 2pm), purely for visual
  sanity-checking on a map.

## Style note

`03_aggregate_by_block_REFERENCE.py` in the repo root is a reference
file from a different, unrelated project (its own old numbering, kept
as-is) -- included only as a style example for comment density and the
"explain why, not just what" convention. It isn't imported or built on
by anything here.
