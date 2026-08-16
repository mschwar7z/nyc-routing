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

The routing engine is self-hosted GraphHopper (Docker, see
`graphhopper/`) over a Manhattan OSM extract, plus a thin FastAPI
backend (`backend/`) that builds the per-request custom_model payload
and forwards it. See "Routing engine" below for how `bike_cost` and
`shade_score` actually get attached -- the join turned out not to be
the OSM-way-tagging approach the architecture note above implies (see
that section for why). No frontend yet.

## Directory structure

```
lib/                     Reusable, testable modules (no I/O)
scripts/                 Orchestrators -- read data/raw, write data/processed
data/raw/                Downloaded source data (see Datasets below)
data/processed/          Script outputs
web/                     Standalone Leaflet pages for visually sanity-checking output
tests/                   Sanity checks for lib/
graphhopper/             Docker setup for the self-hosted GraphHopper routing engine
backend/                 FastAPI wrapper: origin/destination/preference -> GraphHopper route
```

Scripts are numbered by run order, starting at 01.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install pandas shapely geopandas pyproj pyarrow
pip install fastapi "uvicorn[standard]" httpx  # backend/ only
```

Also requires [Docker](https://www.docker.com/) (running, not just
installed) to build and serve the GraphHopper graph -- see "Routing
engine" below.

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

To bring up the routing engine on top of that (see "Routing engine"
below for the full explanation):

```bash
python3 scripts/03_parse_osm_ways.py
python3 scripts/04_validate_block_osm_alignment.py   # QA only, doesn't feed anything downstream

cd graphhopper && docker compose up -d && cd ..        # first boot builds the graph, ~5s for Manhattan
python3 scripts/05_test_custom_models.py                # curls GraphHopper directly, no backend yet

uvicorn backend.main:app --reload --port 8000           # then the actual API
```

Then open `web/index_route.html` directly as a `file://` URL (it only
talks to the backend API, no local data fetches, so it doesn't need a
server the way the other two web pages do) -- click the map to set an
origin and destination, pick a preference, and see GraphHopper's actual
route plus the exact `custom_model` areas that request sent, both
drawn live.

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
  Covers all 12 months (15th of each) x hourly 6am-7pm. Calls
  `solar_position` once per (month, hour) -- not per building, since
  sun angle is effectively uniform citywide at a given moment -- then
  `score_segment` for every block against nearby buildings.

Outputs:
- `data/processed/shade_index.parquet` -- `block_id, month, hour,
  shade_score`. This is the point of the architecture: routing should
  do a fast lookup here instead of recomputing shadows live.
- `data/processed/shade_scored_blocks_jun_2pm.geojson` -- one
  representative snapshot (June, 2pm), purely for visual
  sanity-checking on a map.

## Routing engine

`graphhopper/`, `backend/`, and `scripts/03`-`05` turn the weighting
data above into an actual routable API. Three things the plan going in
assumed turned out not to match reality, found by probing/checking
current docs before writing code against them -- the same "confirm,
don't guess" pattern as the bike-routes resource ID and blockface
street-name issues above.

**OSM extract: Overpass, not Geofabrik.** Geofabrik only publishes a
whole-New-York-*state* PBF (~495MB) -- there's no NYC- or
borough-level extract, so using it would mean downloading the state
and clipping it down with another tool. A literal NYC bounding-box
Overpass query comes back ~570k highway ways (that box also covers
parts of NJ/CT, since it's a rectangle) -- large enough to risk
Overpass's public-instance timeout/rate limits. Scoped to Manhattan
only (~121k ways, chosen over full-NYC or UWS-only -- see below),
Overpass completed cleanly in about a minute. `data/raw/osm/manhattan.osm`
is that pull; `scripts/03_parse_osm_ways.py` parses it into
`data/processed/manhattan_osm_ways.geojson` for the alignment check in
`scripts/04`.

*Why Manhattan, not all 5 boroughs or just the scored UWS box:* the
scored data (`bike_scored_blocks.geojson`, `shade_index.parquet`) only
covers ~889 UWS blocks. All 5 boroughs would mean the vast majority of
the graph has zero score data, for a much larger download/import.
UWS-only would make GraphHopper unable to route through anything
outside that box at all. Manhattan is the middle ground: real
point-to-point routing on a real connected street grid, small enough
to import in seconds.

**No official GraphHopper Docker image exists.** Checked the
`graphhopper/graphhopper` repo tree directly (GitHub API, recursive)
for a Dockerfile -- there isn't one, anywhere. GraphHopper's own docs
only cover building from Maven source or running the release jar with
`java -jar`; every `graphhopper/*` image on Docker Hub is a
third-party community build, often pinned to an older GraphHopper
version. `graphhopper/Dockerfile` instead containerizes the official
prebuilt release jar (11.0) straight from GitHub Releases -- no Maven
build, no compiling GraphHopper's own source, just running an official
binary artifact in a container GraphHopper doesn't publish themselves.

**`bike_cost`/`shade_score` are attached via custom_model `areas`, not
a compiled encoded value.** The architecture note above ("joined onto
GraphHopper's OSM edges") implies tagging OSM ways with our scores as
real per-edge encoded values, referenced directly in a custom_model
expression. Checking GraphHopper's current docs and forum (not
assuming) turned up that this isn't available without modifying
GraphHopper's own source: a custom encoded value needs a compiled
`TagParser` + `EncodedValueFactory` registered in
`DefaultImportRegistry.java`, which lives inside `graphhopper-core`
itself -- not pluggable via config or a mounted jar. It would also bake
`shade_score`'s month/hour buckets in at graph-build time, reintroducing
a rebuild-per-preference problem for shade specifically -- the exact
thing this project picked GraphHopper over Valhalla to avoid.

Instead, `lib/custom_model_areas.py` groups blocks into a handful of
cost buckets (`bike_cost` already only takes 4 discrete values;
`shade_score` is split into 5 quantile buckets for whichever month/hour
is requested), buffers and unions each bucket's block geometry into
one polygon, and passes those as `areas` in the custom_model JSON --
GeoJSON polygons submitted with the request itself, matched to edges
by GraphHopper's own geometry code, no import step or Java involved.
This is fully request-time, same as the rest of the architecture: a
shade query at 8am and one at 2pm rebuild different areas from the
same `shade_index.parquet`, no graph rebuild. The tradeoff is
resolution -- an area is a bucket of blocks, not a single OSM way -- but
forum reports flag GraphHopper's ~100k-character custom_model payload
ceiling and describe hundreds of individual regions as impractical
anyway, so bucketing was the right call independent of the encoded-
value finding.

`scripts/04_validate_block_osm_alignment.py` checks the geometry this
all depends on before building on top of it: 889/889 scored blocks
have a real OSM street within 30ft (max 10.6ft) -- the buffer radius
`lib/custom_model_areas.py` uses (25ft half-width) is sized off that.

**Backend API** (`backend/main.py`): `POST /route` takes `origin`,
`destination` (`[lat, lon]`), and `preference` (`bike`/`shade`/
`default` -- not `traffic`; no traffic dataset exists anywhere in this
repo, see Datasets above), builds the matching custom_model, and
forwards it to GraphHopper. `shade` additionally takes optional
`month`/`hour` (defaults to June/2pm, matching the existing
`shade_scored_blocks_jun_2pm.geojson` snapshot convention) and
`seek_shade` (default `true`; `false` routes toward sun instead). The
response includes the `custom_model`'s `areas` alongside the route
geometry, so a caller can render what actually drove the weighting,
not just the resulting path -- `web/index_route.html` is that
renderer. CORS is wide open (`allow_origins=["*"]`) since that page
calls the API from a `file://` origin; fine for local dev, not
something to carry into a real deployment.

## Style note

`03_aggregate_by_block_REFERENCE.py` in the repo root is a reference
file from a different, unrelated project (its own old numbering, kept
as-is) -- included only as a style example for comment density and the
"explain why, not just what" convention. It isn't imported or built on
by anything here.
